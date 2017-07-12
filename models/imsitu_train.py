"""
Trains the imsitu model for ZSL
"""
from data.imsitu_loader import ImSitu, CudaDataLoader
from config import ModelConfig
from torch import optim
import os
import torch
from lib.misc import CosineRankingLoss, optimize, cosine_ranking_loss, get_ranking
import numpy as np
import time
from torch.nn.utils.rnn import pad_packed_sequence
from lib.imsitu_model import ImsituModel
import pandas as pd
import random
from tqdm import tqdm
from lib.attribute_loss import AttributeLoss
from lib.bce_loss import BCEWithLogitsLoss
from torch.nn import functional as F

# Recommended hyperparameters
args = ModelConfig(lr=1e-4, batch_size=32, eps=1e-8, save_dir='imsitu_train',
                   )
##################################3
# For now we'll use everything: emebddings and w2v.
######################################################3

train_data, val_data, test_data = ImSitu.splits(zeroshot=True)

train_iter, val_iter, test_iter = CudaDataLoader.splits(
    train_data, val_data, test_data, batch_size=args.batch_size, num_workers=2,
    variable_label=False)

att_crit = AttributeLoss(train_data.attributes.domains, size_average=True)
# emb_crit = torch.nn.CrossEntropyLoss(size_average=True)
emb_crit = BCEWithLogitsLoss(size_average=True)

m = ImsituModel(
    zeroshot=True,
    embed_dim=300,
    att_domains=att_crit.domains_per_att,
)
optimizer = optim.Adam(m.parameters(), lr=args.lr, eps=args.eps, betas=(args.beta1, args.beta2))

if torch.cuda.is_available():
    m.cuda()
    att_crit.cuda()
    emb_crit.cuda()
    train_data.attributes.cuda()
    val_data.attributes.cuda()
    test_data.attributes.cuda()


@optimize
def train_batch(x, labels, optimizers=None):
    att_rep, embed_rep = m(x)
    embed_logits = embed_rep @ train_data.attributes.embeds.t()

    att_loss = att_crit(att_rep, train_data.attributes.atts_matrix[labels.data]).sum()

    full_labels = torch.zeros(embed_logits.size(0), embed_logits.size(1)).cuda()
    full_labels.scatter_(1, labels.data[:, None], 1.0)

    embed_loss = emb_crit(embed_logits.view(-1), full_labels.view(-1))
    loss = att_loss + embed_loss
    return loss


def deploy(x, labels, data=val_data):
    att_rep, embed_rep = m(x)
    embed_logits = embed_rep @ data.attributes.embeds.t()
    embed_probs = torch.sigmoid(embed_logits)

    att_probs = []
    start_col = 0
    for gt_col, d_size in enumerate(att_crit.domains_per_att):
        if d_size == 1:
            att_probs.append(torch.sigmoid(att_rep[:, start_col]))
        else:
            att_probs.append(F.softmax(att_rep[:, start_col:(start_col+d_size)]))
        start_col += d_size

    # [batch_size, range size, att size]
    probs_by_att = torch.stack([embed_probs] + att_probs, 2)

    # [batch_size, range size]
    probs_prod = torch.prod(probs_by_att, 2).squeeze()
    denom = probs_prod.sum(1).squeeze()+ 1e-12
    probs = probs_prod / denom.expand_as(probs_prod)


    values, bests = probs.topk(probs.size(1), dim=1)
    _, ranking = bests.topk(bests.size(1), dim=1, largest=False)   # [batch_size, dict_size]
    rank = torch.gather(ranking.data, 1, labels.data[:, None]).cpu().numpy().squeeze()

    top5_bests = bests[:, :5].cpu().data.numpy()

    top1_acc = np.mean(rank==1)
    top5_acc = np.mean(rank<5)
    return top1_acc, top5_acc

last_best_epoch = 1
prev_best = 0.0
for epoch in range(1, 50):
    train_l = []
    val_info = []
    m.eval()
    start_epoch = time.time()
    for val_b, (img_batch, label_batch) in enumerate(val_iter):
        val_info.append(deploy(img_batch, label_batch))

    val_info = pd.DataFrame(np.stack(val_info,0),
                            columns=['loss', 'top1_acc', 'top5_acc']).mean(0)

    print("--- E{:2d} VAL ({:.3f} s/batch) \n {}".format(
        epoch,
        (time.time() - start_epoch)/(len(val_iter) * val_iter.batch_size / train_iter.batch_size),
        val_info), flush=True)

    if val_info['top5_acc'] > prev_best:
        prev_best = val_info['top5_acc']
        last_best_epoch = epoch
    else:
        if last_best_epoch < (epoch - 3):
            print("Early stopping at epoch {}".format(epoch))
            break
    m.train()
    start_epoch = time.time()
    for b, (img_batch, label_batch) in enumerate(train_iter):
        l = train_batch(img_batch, label_batch, optimizers=[optimizer])
        train_l.append(l)
        if b % 100 == 0 and b >= 100:
            print("e{:2d}b{:5d} Cost {:.3f} , {:.3f} s/batch".format(
                epoch, b,
                np.mean(train_l),
                (time.time() - start_epoch) / (b+1),
            ), flush=True)
    print("overall loss was {:.3f}".format(np.mean(train_l)))

torch.save({
    'args': args.args,
    'epoch': epoch,
    'm_state_dict': m.state_dict(),
    'optimizer': optimizer.state_dict(),
}, os.path.join(args.save_dir, 'ckpt_{}.tar'.format(epoch)))
