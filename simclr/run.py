from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import shutil
import time

import torchvision
from torchvision import datasets, transforms

import tensorflow as tf
import torch.nn.functional as F

from simclr import SimCLR
import os
import random
import yaml
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.backends.cudnn as cudnn

from models.resnet_simclr import ResNetSimCLR
from data_aug.dataset_wrapper import DataSetWrapper
import argparse
import nsml
from nsml import DATASET_PATH, IS_ON_NSML
from ImageDataLoader_mixmatch import SimpleImageLoader

NUM_CLASSES = 265


parser = argparse.ArgumentParser(description='Sample Product200K Training')
parser.add_argument('--start_epoch', type=int, default=0, metavar='N', help='number of start epoch (default: 1)')
parser.add_argument('--epochs', type=int, default=300, metavar='N', help='number of epochs to train (default: 200)')

# basic settings
parser.add_argument('--name',default='Res18baseMM', type=str, help='output model name')
parser.add_argument('--gpu_ids',default='0', type=str,help='gpu_ids: e.g. 0  0,1,2  0,2')
parser.add_argument('--batchsize', default=30, type=int, help='batchsize_labeled')
parser.add_argument('--batchsize2', default=75, type=int, help='batchsize_unlabeled')
parser.add_argument('--seed', type=int, default=123, help='random seed')

# basic hyper-parameters
parser.add_argument('--momentum', type=float, default=0.9, metavar='LR', help=' ')
parser.add_argument('--lr', type=float, default=5e-6, metavar='LR', help='learning rate (default: 5e-5)')
parser.add_argument('--imResize', default=256, type=int, help='')
parser.add_argument('--imsize', default=224, type=int, help='')
parser.add_argument('--lossXent', type=float, default=1, help='lossWeight for Xent')

# arguments for logging and backup
parser.add_argument('--log_interval', type=int, default=10, metavar='N', help='logging training status')
parser.add_argument('--save_epoch', type=int, default=50, help='saving epoch interval')

# hyper-parameters for mix-match
parser.add_argument('--alpha', default=0.75, type=float)
parser.add_argument('--lambda-u', default=150, type=float)
parser.add_argument('--T', default=0.5, type=float)

### DO NOT MODIFY THIS BLOCK ###
# arguments for nsml
parser.add_argument('--pause', type=int, default=0)
parser.add_argument('--mode', type=str, default='train')
################################


def top_n_accuracy_score(y_true, y_prob, n=5, normalize=True):
    num_obs, num_labels = y_prob.shape
    idx = num_labels - n - 1
    counter = 0
    argsorted = np.argsort(y_prob, axis=1)
    for i in range(num_obs):
        if y_true[i] in argsorted[i, idx+1:]:
            counter += 1
    if normalize:
        return counter * 1.0 / num_obs
    else:
        return counter

class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()
    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def adjust_learning_rate(opts, optimizer, epoch):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = opts.lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

def linear_rampup(current, rampup_length):
    if rampup_length == 0:
        return 1.0
    else:
        current = np.clip(current / rampup_length, 0.0, 1.0)
        return float(current)

class SemiLoss(object):
    def __call__(self, outputs_x, targets_x, outputs_u, targets_u, epoch, final_epoch):
        probs_u = torch.softmax(outputs_u, dim=1)
        Lx = -torch.mean(torch.sum(F.log_softmax(outputs_x, dim=1) * targets_x, dim=1))
        Lu = torch.mean((probs_u - targets_u)**2)
        return Lx, Lu, opts.lambda_u #* linear_rampup(epoch, final_epoch)

def interleave_offsets(batch, nu):
    groups = [batch // (nu + 1)] * (nu + 1)
    for x in range(batch - sum(groups)):
        groups[-x - 1] += 1
    offsets = [0]
    for g in groups:
        offsets.append(offsets[-1] + g)
    assert offsets[-1] == batch
    return offsets

def interleave(xy, batch):
    nu = len(xy) - 1
    offsets = interleave_offsets(batch, nu)
    xy = [[v[offsets[p]:offsets[p + 1]] for p in range(nu + 1)] for v in xy]
    for i in range(1, nu + 1):
        xy[0][i], xy[i][i] = xy[i][i], xy[0][i]
    return [torch.cat(v, dim=0) for v in xy]


def _infer(model, root_path, test_loader=None):
    if test_loader is None:
        test_loader = torch.utils.data.DataLoader(
            SimpleImageLoader(root_path, 'test',
                               transform=transforms.Compose([
                                   transforms.Resize(opts.imResize),
                                   transforms.CenterCrop(opts.imsize),
                                   transforms.ToTensor(),
                                   transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                               ])), batch_size=opts.batchsize, shuffle=False, num_workers=4, pin_memory=True)
        print('loaded {} test images'.format(len(test_loader.dataset)))

    outputs = []
    s_t = time.time()
    for idx, image in enumerate(test_loader):
        if torch.cuda.is_available():
            image = image.cuda()
        _, probs = model(image)
        output = torch.argmax(probs, dim=1)
        output = output.detach().cpu().numpy()
        outputs.append(output)

    outputs = np.concatenate(outputs)
    return outputs

def bind_nsml(model):
    def save(dir_name, *args, **kwargs):
        os.makedirs(dir_name, exist_ok=True)
        state = model.state_dict()
        torch.save(state, os.path.join(dir_name, 'model.pt'))
        print('saved')

    def load(dir_name, *args, **kwargs):
        state = torch.load(os.path.join(dir_name, 'model.pt'))
        model.load_state_dict(state)
        print('loaded')

    def infer(root_path):
        return _infer(model, root_path)

    nsml.bind(save=save, load=load, infer=infer)

def split_ids(path, ratio):
    with open(path) as f:
        ids_l = []
        ids_u = []
        for i, line in enumerate(f.readlines()):
            if i == 0 or line == '' or line == '\n':
                continue
            line = line.replace('\n', '').split('\t')
            if int(line[1]) >= 0:
                ids_l.append(int(line[0]))
            else:
                ids_u.append(int(line[0]))

    ids_l = np.array(ids_l)
    ids_u = np.array(ids_u)

    perm = np.random.permutation(np.arange(len(ids_l)))
    cut = int(ratio*len(ids_l))
    train_ids = ids_l[perm][cut:]
    val_ids = ids_l[perm][:cut]

    return train_ids, val_ids, ids_u

def main():
    print("SimCLR Phase")
    global opts
    opts = parser.parse_args()

    seed = 123
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    print(torch.cuda.device_count())


    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    use_gpu = torch.cuda.is_available()
    if use_gpu:
        #opts.cuda = 1
        print("Currently using GPU {}".format('0'))
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(seed)
    else:
        print("Currently using CPU (GPU is highly recommended)")

    config = yaml.load(open("config.yaml", "r"), Loader=yaml.FullLoader)
    dataset = DataSetWrapper(config['batch_size'], **config['dataset'])

    # model = ResNetSimCLR(config["model"], 256).to('cuda')
    # #model = ResNetSimCLR("resnet50",256).to('cuda')
    # model = self._load_pre_trained_weights(model)




    simclr = SimCLR(dataset, config)
    model = simclr.train()

    # ### DO NOT MODIFY THIS BLOCK ###
    # if IS_ON_NSML:
    #     bind_nsml(model)
    #     if opts.pause:
    #         nsml.paused(scope=locals())
    # ################################

    # for param in model.parameters():
    #     param.requires_grad = False

    '''
    Change model structure, classification ~~
    '''
    print("SimCLR Phase Ended")
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print('  + Number of params in SimCLR: {}'.format(params))


    self.features.requires_grad = False
    print("Turned of gradients for all layers except classifier")


    print("MixMatch Phase")
    parameters = filter(lambda p: p.requires_grad, model.parameters())
    n_parameters = sum([p.data.nelement() for p in model.parameters()])
    params2 = sum([np.prod(p.size()) for p in parameters])
    print('  + Total Number of params in MixMatch: {}'.format(n_parameters))
    print('  + Number of params to train in MixMatch: {}'.format(params2))

    if opts.mode == 'train':
        model.train()
        # Set dataloader
        #nsml.load(checkpoint = 'Res18baseMM_best', session = 'kaist_15/fashion_eval/162')
        train_ids, val_ids, unl_ids = split_ids(os.path.join(DATASET_PATH, 'train/train_label'), 0.2)
        print('found {} train, {} validation and {} unlabeled images'.format(len(train_ids), len(val_ids), len(unl_ids)))
        train_loader_mm = torch.utils.data.DataLoader(
            SimpleImageLoader(DATASET_PATH, 'train', train_ids,
                              transform=transforms.Compose([
                                  transforms.Resize(opts.imResize),
                                  transforms.RandomResizedCrop(opts.imsize),
                                  transforms.RandomHorizontalFlip(),
                                  transforms.RandomVerticalFlip(),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),])),
                                batch_size=opts.batchsize, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
        print('train_loader done')

        unlabel_loader_mm = torch.utils.data.DataLoader(
            SimpleImageLoader(DATASET_PATH, 'unlabel', unl_ids,
                              transform=transforms.Compose([
                                  transforms.Resize(opts.imResize),
                                  transforms.RandomResizedCrop(opts.imsize),
                                  transforms.RandomHorizontalFlip(),
                                  transforms.RandomVerticalFlip(),
                                  transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),])),
                                batch_size=opts.batchsize2, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
        print('unlabel_loader done')

        validation_loader_mm = torch.utils.data.DataLoader(
            SimpleImageLoader(DATASET_PATH, 'val', val_ids,
                               transform=transforms.Compose([
                                   transforms.Resize(opts.imResize),
                                   transforms.CenterCrop(opts.imsize),
                                   transforms.ToTensor(),
                                   transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),])),
                               batch_size=opts.batchsize2, shuffle=False, num_workers=4, pin_memory=True, drop_last=False)
        print('validation_loader done')

        optimizer = optim.SGD(model.parameters(), lr=opts.lr, momentum = opts.momentum, weight_decay = 0.0004)

        # INSTANTIATE LOSS CLASS
        train_criterion = SemiLoss()

        # INSTANTIATE STEP LEARNING SCHEDULER CLASS
        #scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,  milestones=[50, 150], gamma=0.1)

        '''
        !!!!!!!!!!!!!
        실험에 대한 정보 최대한 자세히 적기!!!
        코드 다 저장해놓을순 없으니까 나중에 NSML 터미널만 보고도 무슨 실험인지 알 수 있게!
        귀찮더라도..
        !!!!!!!!!!!
        '''

        print("Title: {}".format("Fix - MixMatch"))
        print("Purpose: {}".format("MixMatch with threshold policy adaped from fixmatch(Chocolatefudge)"))
        print("Environments")
        print("Model: {}".format("Resnet 50"))
        print("Hyperparameters: batchsize {}, lr {}, epoch {}, lambdau {}".format(opts.batchsize, opts.lr, opts.epochs, opts.lambda_u))
        print("Optimizer: {}, Scheduler: {}".format("SGD with momentum 0.9, wd 0.0004", "No Schedule"))
        print("Other necessary Hyperparameters: {}".format("Batchsize for unlabeled is 75., lambda-u not changed in overall training step"))
        print("Details: {}".format("Experiment for thresholding some unlabeled examples in pseudo labeling. current threshold 0.9, Continue Learning for 250~400 epoches"))
        print("Etc: {}".format("No interleaving. IDK // ***T=0.5 is applied for unlabeled data // threshold scheduling with epoch (0.9 0.875 0.85 0.825 0.8 ... 0.4) for every 5 epochs.  Without learning rate scheduling"))




        # Train and Validation
        best_acc = -1
        for epoch in range(opts.start_epoch, opts.epochs + 1):
            print('start training')
            loss, _, _ = train(opts, train_loader_mm, unlabel_loader_mm, model, train_criterion, optimizer, epoch, use_gpu)
            #scheduler.step()

            print('start validation')
            acc_top1, acc_top5 = validation(opts, validation_loader_mm, model, epoch, use_gpu)
            is_best = acc_top1 > best_acc
            best_acc = max(acc_top1, best_acc)
            nsml.report(summary=True, train_loss= loss, val_acc_top1= acc_top1, val_acc_top5=acc_top5, step=epoch)
            if is_best:
                print('saving best checkpoint...')
                if IS_ON_NSML:
                    nsml.save(opts.name + '_best')
                else:
                    torch.save(model.state_dict(), os.path.join('runs', opts.name + '_best'))
            if (epoch + 1) % opts.save_epoch == 0:
                if IS_ON_NSML:
                    nsml.save(opts.name + '_e{}'.format(epoch))
                else:
                    torch.save(model.state_dict(), os.path.join('runs', opts.name + '_e{}'.format(epoch)))



def train(opts, train_loader, unlabel_loader, model, criterion, optimizer, epoch, use_gpu):
    losses = AverageMeter()
    losses_x = AverageMeter()
    losses_un = AverageMeter()
    good_ulb = AverageMeter()
    weight_scale = AverageMeter()
    acc_top1 = AverageMeter()
    acc_top5 = AverageMeter()
    avg_loss = 0.0
    avg_top1 = 0.0
    avg_top5 = 0.0

    model.train()

    nCnt =0
    labeled_train_iter = iter(train_loader)
    unlabeled_train_iter = iter(unlabel_loader)

    for batch_idx in range(len(train_loader)):
        try:
            data = labeled_train_iter.next()
            inputs_x, targets_x = data
        except:
            labeled_train_iter = iter(train_loader)
            data = labeled_train_iter.next()
            inputs_x, targets_x = data
        try:
            data = unlabeled_train_iter.next()
            inputs_u1, inputs_u2 = data
        except:
            unlabeled_train_iter = iter(unlabel_loader)
            data = unlabeled_train_iter.next()
            inputs_u1, inputs_u2 = data

        batch_size = inputs_x.size(0)
        batch_size_u = inputs_u1.size(0)
        # Transform label to one-hot
        classno = NUM_CLASSES
        targets_org = targets_x
        targets_x = torch.zeros(batch_size, classno).scatter_(1, targets_x.view(-1,1), 1)

        if use_gpu :
            inputs_x, targets_x = inputs_x.cuda(), targets_x.cuda()
            inputs_u1, inputs_u2 = inputs_u1.cuda(), inputs_u2.cuda()
        inputs_x, targets_x = Variable(inputs_x), Variable(targets_x)
        inputs_u1, inputs_u2 = Variable(inputs_u1), Variable(inputs_u2)

        threshold = max(0.9 - (epoch//5)/40, 0.5)
        rm_idx = []

        with torch.no_grad():
            # compute guessed labels of unlabel samples
            embed_u1, pred_u1 = model(inputs_u1)
            embed_u2, pred_u2 = model(inputs_u2)
            #print(pred_u1.size())
            pred_u_all = (torch.softmax(pred_u1, dim=1) + torch.softmax(pred_u2, dim=1)) / 2
            crit = torch.max(pred_u_all, axis=1) #batch size
            for i in range(int(crit[0].shape[0])):
                if crit[0][i]<threshold:
                    rm_idx.append(i)

            #print(pred_u2.size())
            pt = pred_u_all**(1/opts.T)
            #pt = pred_u_all
            targets_u = pt / pt.sum(dim=1, keepdim=True)
            targets_u = targets_u.detach()

        #print(inputs_u1.size())
        ##print(rm_idx)
        inputs_u1 = np.delete(inputs_u1.cpu(), rm_idx, axis=0)
        inputs_u2 = np.delete(inputs_u2.cpu(), rm_idx, axis=0)
        targets_u = np.delete(targets_u.cpu(), rm_idx, axis=0)
        good_ulb.update(batch_size_u - len(rm_idx))
        #print(inputs_u1.size())
        #print(targets_u.size())

        inputs_u1, inputs_u2, targets_u = inputs_u1.cuda(), inputs_u2.cuda(), targets_u.cuda()
        # mixup

        all_inputs = torch.cat([inputs_x, inputs_u1, inputs_u2], dim=0)
        all_targets = torch.cat([targets_x, targets_u, targets_u], dim=0)
        #print(all_inputs.size())
        #print(all_targets.size())


        lamda = np.random.beta(opts.alpha, opts.alpha)
        lamda= max(lamda, 1-lamda)
        newidx = torch.randperm(all_inputs.size(0))
        input_a, input_b = all_inputs, all_inputs[newidx]
        target_a, target_b = all_targets, all_targets[newidx]

        mixed_input = lamda * input_a + (1 - lamda) * input_b
        mixed_target = lamda * target_a + (1 - lamda) * target_b
        #print(mixed_input.size())
        #print(mixed_target.size())

        # interleave labeled and unlabed samples between batches to get correct batchnorm calculation
        mixed_input = list(torch.split(mixed_input, batch_size))
        #mixed_input = interleave(mixed_input, batch_size)
        #print(len(mixed_input))

        optimizer.zero_grad()

        fea, logits_temp = model(mixed_input[0])

        if len(rm_idx)!=batch_size_u:
            #print("asdlfkjasodifjasio")
            logits = [logits_temp]
            for newinput in mixed_input[1:]:
                fea, logits_temp = model(newinput)
                logits.append(logits_temp)

            # put interleaved samples back
            #logits = interleave(logits, batch_size)
            logits_x = logits[0]
            logits_u = torch.cat(logits[1:], dim=0)
            #print(logits_x.size())
            #print(logits_u.size())

            loss_x, loss_un, weigts_mixing = criterion(logits_x, mixed_target[:batch_size], logits_u, mixed_target[batch_size:], epoch+batch_idx/len(train_loader), opts.epochs)
            loss = loss_x + weigts_mixing * loss_un
            losses.update(loss.item(), inputs_x.size(0))
            losses_x.update(loss_x.item(), inputs_x.size(0))
            losses_un.update(loss_un.item(), inputs_x.size(0))
            weight_scale.update(weigts_mixing, inputs_x.size(0))

        else:
            logits = [logits_temp]
            #logits = interleave(logits, batch_size)
            logits_x = logits[0]
            loss_x = -torch.mean(torch.sum(F.log_softmax(logits_x, dim=1) * targets_x, dim=1))
            loss = loss_x
            losses.update(loss.item(), inputs_x.size(0))
            losses_x.update(loss_x.item(), inputs_x.size(0))
            losses_un.update(0, inputs_x.size(0))
            weight_scale.update(75, inputs_x.size(0))



        # compute gradient and do SGD step
        loss.backward()
        optimizer.step()

        with torch.no_grad():
            # compute guessed labels of unlabel samples
            embed_x, pred_x1 = model(inputs_x)

        acc_top1b = top_n_accuracy_score(targets_org.data.cpu().numpy(), pred_x1.data.cpu().numpy(), n=1)*100
        acc_top5b = top_n_accuracy_score(targets_org.data.cpu().numpy(), pred_x1.data.cpu().numpy(), n=5)*100
        acc_top1.update(torch.as_tensor(acc_top1b), inputs_x.size(0))
        acc_top5.update(torch.as_tensor(acc_top5b), inputs_x.size(0))

        avg_loss += loss.item()
        avg_top1 += acc_top1b
        avg_top5 += acc_top5b

        if batch_idx % opts.log_interval == 0:
            print('Train Epoch:{} [{}/{}] Loss:{:.4f}({:.4f}) Top-1:{:.2f}%({:.2f}%) Top-5:{:.2f}%({:.2f}%) '.format(
                epoch, batch_idx *inputs_x.size(0), len(train_loader.dataset), losses.val, losses.avg, acc_top1.val, acc_top1.avg, acc_top5.val, acc_top5.avg))

        nCnt += 1

    avg_loss =  float(avg_loss/nCnt)
    avg_top1 = float(avg_top1/nCnt)
    avg_top5 = float(avg_top5/nCnt)

    nsml.report(summary=True, train_acc_top1= avg_top1, train_acc_top5=avg_top5, step=epoch, good_unlabeled = good_ulb.avg)
    return  avg_loss, avg_top1, avg_top5


def validation(opts, validation_loader, model, epoch, use_gpu):
    model.eval()
    avg_top1= 0.0
    avg_top5 = 0.0
    nCnt =0
    with torch.no_grad():
        for batch_idx, data in enumerate(validation_loader):
            inputs, labels = data
            if use_gpu :
                inputs = inputs.cuda()
            inputs = Variable(inputs)
            nCnt +=1
            embed_fea, preds = model(inputs)

            acc_top1 = top_n_accuracy_score(labels.numpy(), preds.data.cpu().numpy(), n=1)*100
            acc_top5 = top_n_accuracy_score(labels.numpy(), preds.data.cpu().numpy(), n=5)*100
            avg_top1 += acc_top1
            avg_top5 += acc_top5

        avg_top1 = float(avg_top1/nCnt)
        avg_top5= float(avg_top5/nCnt)
        print('Test Epoch:{} Top1_acc_val:{:.2f}% Top5_acc_val:{:.2f}% '.format(epoch, avg_top1, avg_top5))
    return avg_top1, avg_top5

if __name__ == "__main__":
    main()
