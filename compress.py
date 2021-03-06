import argparse
import math
import random
import os

import numpy as np
import torch
import torch.distributed as dist
import lpips
from torch import nn, autograd, optim
from torch.nn import functional as F
from torch.utils import data
from torchvision import transforms, utils
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter

try:
    import wandb
except ImportError:
    wandb = None

from dataset import MultiResolutionDataset
from distributed import (
    get_rank,
    synchronize,
    reduce_loss_dict,
    reduce_sum,
    get_world_size,
)
from op import conv2d_gradfix
from non_leaking import augment, AdaptiveAugment


def data_sampler(dataset, shuffle, distributed):
    if distributed:
        return data.distributed.DistributedSampler(dataset, shuffle=shuffle)

    if shuffle:
        return data.RandomSampler(dataset)

    else:
        return data.SequentialSampler(dataset)


def requires_grad(model, flag=True):
    for p in model.parameters():
        p.requires_grad = flag


def accumulate(model1, model2, decay=0.999):
    par1 = dict(model1.named_parameters())
    par2 = dict(model2.named_parameters())

    for k in par1.keys():
        par1[k].data.mul_(decay).add_(par2[k].data, alpha=1 - decay)


def sample_data(loader):
    while True:
        for batch in loader:
            yield batch


def d_logistic_loss(real_pred, fake_pred):
    real_loss = F.softplus(-real_pred)
    fake_loss = F.softplus(fake_pred)

    return real_loss.mean() + fake_loss.mean()


def d_r1_loss(real_pred, real_img):
    with conv2d_gradfix.no_weight_gradients():
        grad_real, = autograd.grad(
            outputs=real_pred.sum(), inputs=real_img, create_graph=True
        )
    grad_penalty = grad_real.pow(2).reshape(grad_real.shape[0], -1).sum(1).mean()

    return grad_penalty


def g_nonsaturating_loss(fake_pred):
    loss = F.softplus(-fake_pred).mean()

    return loss


def g_path_regularize(fake_img, latents, mean_path_length, decay=0.01):
    noise = torch.randn_like(fake_img) / math.sqrt(
        fake_img.shape[2] * fake_img.shape[3]
    )
    grad, = autograd.grad(
        outputs=(fake_img * noise).sum(), inputs=latents, create_graph=True
    )
    path_lengths = torch.sqrt(grad.pow(2).sum(2).mean(1))

    path_mean = mean_path_length + decay * (path_lengths.mean() - mean_path_length)

    path_penalty = (path_lengths - path_mean).pow(2).mean()

    return path_penalty, path_mean.detach(), path_lengths


def make_noise(batch, latent_dim, n_noise, device):
    if n_noise == 1:
        return torch.randn(batch, latent_dim, device=device)

    noises = torch.randn(n_noise, batch, latent_dim, device=device).unbind(0)

    return noises


def mixing_noise(batch, latent_dim, prob, device):
    if prob > 0 and random.random() < prob:
        return make_noise(batch, latent_dim, 2, device)

    else:
        return [make_noise(batch, latent_dim, 1, device)]


def set_grad_none(model, targets):
    for n, p in model.named_parameters():
        if n in targets:
            p.grad = None

# kernel alignment for knowledge distillation
# find similarity index between 2 tensors
def KA(X, Y):
    X_ = X.view(X.size(0), -1)
    Y_ = Y.view(Y.size(0), -1)
    assert X_.shape[0] == Y_.shape[
        0], f'X_ and Y_ must have the same shape on dim 0, but got {X_.shape[0]} for X_ and {Y_.shape[0]} for Y_.'
    X_vec = X_ @ X_.T
    Y_vec = Y_ @ Y_.T
    ret = (X_vec * Y_vec).sum() / ((X_vec**2).sum() * (Y_vec**2).sum())**0.5
    return ret

# train student model
def train(args, loader, generator, discriminator, student_generator, student_discriminator, g_optim, d_optim, g_ema, student_g_ema, device):
    # create directories
    save_dir = args.expr_dir
    os.makedirs(save_dir, 0o777, exist_ok=True)
    os.makedirs(save_dir + "/checkpoints", 0o777, exist_ok=True)
    os.makedirs(save_dir + "/sample", 0o777, exist_ok=True)
    
    loader = sample_data(loader)
    pbar = range(args.iter)

    if get_rank() == 0:
        pbar = tqdm(pbar, initial=args.start_iter, dynamic_ncols=True, smoothing=0.01)

    mean_path_length = 0

    # create lips model
    loss_fn_vgg = lpips.PerceptualLoss(net='vgg')

    d_loss_val = 0
    r1_loss = torch.tensor(0.0, device=device)
    g_loss_val = 0
    path_loss = torch.tensor(0.0, device=device)
    path_lengths = torch.tensor(0.0, device=device)
    mean_path_length_avg = 0
    loss_dict = {}

    if args.distributed:
        g_module = student_generator.module
        d_module = student_discriminator.module
    else:
        g_module = student_generator
        d_module = student_discriminator

    accum = 0.5 ** (32 / (10 * 1000))
    ada_aug_p = args.augment_p if args.augment_p > 0 else 0.0
    r_t_stat = 0

    if args.augment and args.augment_p == 0:
        ada_augment = AdaptiveAugment(args.ada_target, args.ada_length, 8, device)

    sample_z = torch.randn(args.n_sample, args.latent, device=device)

    for idx in pbar:
        i = idx + args.start_iter

        if i > args.iter:
            print("Done!")
            break

        real_img = next(loader)
        real_img = real_img.to(device)

                            ###########################
                            ### Train Discriminator ###
                            ###########################

        requires_grad(generator, False)
        requires_grad(student_generator, False)
        requires_grad(student_discriminator, True)

        # generate noise and fake image with it
        noise = mixing_noise(args.batch, args.latent, args.mixing, device)
        fake_img_s, _, _ = student_generator(noise)

        ## Perform data augmentation if augment is set in arguments
        if args.augment:
            real_img_aug, _ = augment(real_img, ada_aug_p)
            fake_img_s, _ = augment(fake_img_s, ada_aug_p)
        else:
            real_img_aug = real_img

        # real_img_aug = F.interpolate(real_img_aug, args.size_s, mode="bilinear")

        # make prediction
        fake_pred = student_discriminator(fake_img_s)
        real_pred = student_discriminator(real_img_aug)

        d_loss = d_logistic_loss(real_pred, fake_pred)

        loss_dict['d'] = d_loss
        loss_dict["real_score"] = real_pred.mean()
        loss_dict["fake_score"] = fake_pred.mean()

        student_discriminator.zero_grad()
        d_loss.backward()
        d_optim.step()

        # augmentation to real prediction
        if args.augment and args.augment_p == 0:
            ada_aug_p = ada_augment.tune(real_pred)
            r_t_stat = ada_augment.r_t_stat

        # Discriminator regularisation
        d_regularize = i % args.d_reg_every == 0
        if d_regularize:
            real_img.requires_grad = True

            if args.augment:
                real_img_aug, _ = augment(real_img, ada_aug_p)
            else:
                real_img_aug = real_img

            # real_img_aug = F.interpolate(real_img_aug, args.size_s, mode="bilinear")

            real_pred = student_discriminator(real_img_aug)
            r1_loss = d_r1_loss(real_pred, real_img)

            student_discriminator.zero_grad()
            (args.r1 / 2 * r1_loss * args.d_reg_every + 0 * real_pred[0]).backward()

            d_optim.step()

        loss_dict["r1"] = r1_loss
                            
                            ##################################
                            ### End of Train Discriminator ###
                            ##################################

                                #######################
                                ### Train Generator ###
                                #######################

        requires_grad(student_generator, True)
        requires_grad(generator, True)
        requires_grad(student_discriminator, False)

        noise = mixing_noise(args.batch, args.latent, args.mixing, device)
        fake_img_t, _, f_maps_t = generator(noise, return_f_maps=True)
        fake_img_s, _, f_maps_s = student_generator(noise, return_f_maps=True)

        if args.augment:
            fake_img_t, _ = augment(fake_img_t, ada_aug_p)
            fake_img_s, _ = augment(fake_img_s, ada_aug_p)

        # upsample the output of the student generator
        down_fake_img_t = F.interpolate(fake_img_t, args.size_s, mode="bilinear")

        # adverserial loss
        fake_pred_s = student_discriminator(fake_img_s)
        g_loss = g_nonsaturating_loss(fake_pred_s)

        # Kernel Alignment
        if args.kernel_alignment:
            dist_loss = 0 # for knowledge distillation
            for f_s, f_t in zip(f_maps_s, f_maps_t):
                dist_loss += KA(f_s, f_t)
            dist_loss = -dist_loss # we want to maximise it
            g_loss = g_loss + dist_loss

        # Perceptual Loss
        # causes stack expects each tensor to be equal size, but got [4, 1, 1, 1] at entry 0 and [] at entry 1
        # error in distributed setting.
        if args.perc_loss:
            perc_loss = 0
            perc_loss = loss_fn_vgg(fake_img_s, down_fake_img_t)
            g_loss = g_loss + perc_loss.mean()

        # adv + perc + ka
        loss_dict["g"] = g_loss

        student_generator.zero_grad()
        g_loss.backward()
        g_optim.step()

        g_regularize = i % args.g_reg_every == 0

        if g_regularize:
            path_batch_size = max(1, args.batch // args.path_batch_shrink)
            noise = mixing_noise(path_batch_size, args.latent, args.mixing, device)
            fake_img, latents, _ = student_generator(noise, return_latents=True)

            path_loss, mean_path_length, path_lengths = g_path_regularize(
                fake_img, latents, mean_path_length
            )

            student_generator.zero_grad()
            weighted_path_loss = args.path_regularize * args.g_reg_every * path_loss

            if args.path_batch_shrink:
                weighted_path_loss += 0 * fake_img[0, 0, 0, 0]

            weighted_path_loss.backward()

            g_optim.step()

            mean_path_length_avg = (
                reduce_sum(mean_path_length).item() / get_world_size()
            )

                                ##############################
                                ### End of Train Generator ###
                                ##############################
        loss_dict["path"] = path_loss
        loss_dict["path_length"] = path_lengths.mean()

        accumulate(student_g_ema, g_module, accum)

        loss_reduced = reduce_loss_dict(loss_dict)

        g_loss_val = loss_reduced["g"].mean().item()
        d_loss_val = loss_reduced["d"].mean().item()
        path_loss_val = loss_reduced["path"].mean().item()
        path_length_val = loss_reduced["path_length"].mean().item()
        r1_val = loss_reduced["r1"].mean().item()
        real_score_val = loss_reduced["real_score"].mean().item()
        fake_score_val = loss_reduced["fake_score"].mean().item()

        if get_rank() == 0:
            pbar.set_description(
                (
                    f"d: {d_loss_val:.4f}; g: {g_loss_val:.4f}; r1: {r1_val:.4f}; "
                    f"path: {path_loss_val:.4f}; mean path: {mean_path_length_avg:.4f}; "
                    f"augment: {ada_aug_p:.4f}"
                )
            )

            if wandb and args.wandb:
                wandb.log(
                    {
                        "Generator": g_loss_val,
                        "Discriminator": d_loss_val,
                        "Augment": ada_aug_p,
                        "Rt": r_t_stat,
                        "R1": r1_val,
                        "Path Length Regularization": path_loss_val,
                        "Mean Path Length": mean_path_length,
                        "Real Score": real_score_val,
                        "Fake Score": fake_score_val,
                        "Path Length": path_length_val,
                    }
                )

            if i % 1000 == 0:
                with torch.no_grad():
                    student_g_ema.eval()
                    sample, _, _ = student_g_ema([sample_z])
                    utils.save_image(
                        sample,
                        f"{save_dir}/sample/{str(i).zfill(6)}-student.png",
                        nrow=int(args.n_sample ** 0.5),
                        normalize=True,
                        range=(-1, 1),
                    )

            if i % 10000 == 0:
                torch.save(
                    {
                        "g": g_module.state_dict(),
                        "d": d_module.state_dict(),
                        "g_ema": student_g_ema.state_dict(),
                        "g_optim": g_optim.state_dict(),
                        "d_optim": d_optim.state_dict(),
                        "args": args,
                        "ada_aug_p": ada_aug_p,
                    },
                    f"{save_dir}/checkpoints/{str(i).zfill(6)}.pt",
                )
                


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="StyleGAN2 compression")
    parser.add_argument("--path", type=str, help="path to the lmdb dataset")
    parser.add_argument('--arch', type=str, default='stylegan2', help='model architectures (stylegan2 | swagan)')
    parser.add_argument("--iter", type=int, default=800000, help="total training iterations")
    parser.add_argument("--batch", type=int, default=16, help="batch sizes for each gpus")
    parser.add_argument("--n_sample",type=int,default=64,help="number of the samples generated during training",)
    parser.add_argument("--size", type=int, default=256, help="image sizes for the model")
    parser.add_argument("--size_s", type=int, default=256, help="image sizes for the student model")
    parser.add_argument("--r1", type=float, default=10, help="weight of the r1 regularization")
    parser.add_argument("--path_regularize",type=float,default=2,help="weight of the path length regularization",)
    parser.add_argument("--path_batch_shrink",type=int,default=2,help="batch size reducing factor for the path length regularization (reduce memory consumption)",)
    parser.add_argument("--d_reg_every",type=int,default=16,help="interval of the applying r1 regularization",)
    parser.add_argument("--g_reg_every",type=int,default=4,help="interval of the applying path length regularization",)
    parser.add_argument("--mixing", type=float, default=0.9, help="probability of latent code mixing")
    parser.add_argument("--ckpt",type=str,default=None,help="path to the checkpoints to resume training",)
    parser.add_argument("--ckpt_s",type=str,default=None,help="path to the checkpoints to resume training of the student network",)
    parser.add_argument("--lr", type=float, default=0.002, help="learning rate")
    parser.add_argument("--channel_multiplier",type=int,default=2,help="channel multiplier factor for the model. config-f = 2, else = 1",)
    parser.add_argument("--channel_multiplier_s",type=int,default=1,help="channel multiplier factor for the student model. config-f = 2, else = 1",)
    parser.add_argument("--wandb", action="store_true", help="use weights and biases logging")
    parser.add_argument("--local_rank", type=int, default=0, help="local rank for distributed training")
    parser.add_argument("--augment", action="store_true", help="apply non leaking augmentation")
    parser.add_argument("--augment_p",type=float,default=0,help="probability of applying augmentation. 0 = use adaptive augmentation",)
    parser.add_argument("--ada_target",type=float,default=0.6,help="target augmentation probability for adaptive augmentation",)
    parser.add_argument("--ada_length",type=int,default=500 * 1000,help="target duraing to reach augmentation probability for adaptive augmentation",)
    parser.add_argument("--ada_every",type=int,default=256,help="probability update interval of the adaptive augmentation",)
    parser.add_argument("--kernel_alignment", action="store_true", default=False, help="Perform kernel alignment for knowledge distillation.")
    parser.add_argument("--perc_loss", action="store_true", default=False, help="Perform perceptual loss to increase similarity of generated images.")
    parser.add_argument("--inherit_style", action="store_true", default=False, help="Inherit parent style weight.")
    parser.add_argument("--expr_dir", type=str, default='./expr', help="Define directory where checkpoints and samples will be stored.")
    parser.add_argument("--gpu",type=str,default="cuda",help="select gpu id",)

    args = parser.parse_args()

    device = f'cuda:{args.gpu}'

    n_gpu = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    args.distributed = n_gpu > 1

    if args.distributed:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
        synchronize()

    args.latent = 512
    args.n_mlp = 8
    args.start_iter = 0

    if args.arch == 'stylegan2':
        from model import Generator, Discriminator
    elif args.arch == 'swagan':
        from swagan import Generator, Discriminator

    # Teacher network
    generator = Generator(
        args.size, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier
    ).to(device)

    discriminator = Discriminator(
        args.size, channel_multiplier=args.channel_multiplier
    ).to(device)

    g_ema = Generator(
        args.size, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier
    ).to(device)
    g_ema.eval()
    accumulate(g_ema, generator, 0)

    g_reg_ratio = args.g_reg_every / (args.g_reg_every + 1)
    d_reg_ratio = args.d_reg_every / (args.d_reg_every + 1)

    # Student network
    student_generator = Generator(
        args.size_s, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier_s
    ).to(device)

    g_optim = optim.Adam(
        student_generator.parameters(),
        lr=args.lr * g_reg_ratio,
        betas=(0 ** g_reg_ratio, 0.99 ** g_reg_ratio),
    )

    student_discriminator = Discriminator(
        args.size_s, channel_multiplier=args.channel_multiplier_s
    ).to(device)

    d_optim = optim.Adam(
        student_discriminator.parameters(),
        lr=args.lr * d_reg_ratio,
        betas=(0 ** d_reg_ratio, 0.99 ** d_reg_ratio),
    )

    student_g_ema = Generator(
        args.size_s, args.latent, args.n_mlp, channel_multiplier=args.channel_multiplier_s
    ).to(device)
    student_g_ema.eval()
    accumulate(student_g_ema, student_generator, 0)

    # load teacher network
    if args.ckpt is not None:
        print("load model:", args.ckpt)

        ckpt = torch.load(args.ckpt, map_location=lambda storage, loc: storage)

        try:
            ckpt_name = os.path.basename(args.ckpt)
            # args.start_iter = int(os.path.splitext(ckpt_name)[0])
        except ValueError:
            pass
        
        generator.load_state_dict(ckpt["g"], strict=False)
        discriminator.load_state_dict(ckpt["d"])
        g_ema.load_state_dict(ckpt["g_ema"], strict=False)

        # g_optim.load_state_dict(ckpt["g_optim"])
        # d_optim.load_state_dict(ckpt["d_optim"])

        if args.inherit_style:
            style_dict = { k: v for k,v in generator.state_dict().items() if 'style' in k }
            student_generator.load_state_dict(style_dict, strict=False)
            style_dict = { k: v for k,v in g_ema.state_dict().items() if 'style' in k }
            student_g_ema.load_state_dict(style_dict, strict=False)

    # continue training student network
    if args.ckpt_s is not None:
        print(f"load student model: { args.ckpt_s }")
        ckpt_s = torch.load(args.ckpt_s, map_location=lambda storage, loc: storage)
        try:
            ckpt_s_name = os.path.basename(args.ckpt_s)
            args.start_iter = int(os.path.splitext(ckpt_s_name)[0])
        except ValueError:
            pass
        student_generator.load_state_dict(ckpt["g"])
        student_g_ema.load_state_dict(ckpt["g_ema"])

        g_optim.load_state_dict(ckpt_s["g_optim"])
        d_optim.load_state_dict(ckpt_s["d_optim"])

    # for distributed training
    if args.distributed:
        generator = nn.parallel.DistributedDataParallel(
            generator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            broadcast_buffers=False,
        )

        discriminator = nn.parallel.DistributedDataParallel(
            discriminator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            broadcast_buffers=False,
        )

        student_generator = nn.parallel.DistributedDataParallel(
            student_generator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            broadcast_buffers=False,
        )

        student_discriminator = nn.parallel.DistributedDataParallel(
            student_discriminator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            broadcast_buffers=False,
        )

    transform = transforms.Compose(
        [
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True),
        ]
    )

    dataset = MultiResolutionDataset(args.path, transform, args.size_s) # load student size
    loader = data.DataLoader(
        dataset,
        batch_size=args.batch,
        sampler=data_sampler(dataset, shuffle=True, distributed=args.distributed),
        drop_last=True,
    )

    if get_rank() == 0 and wandb is not None and args.wandb:
        wandb.init(project="stylegan 2")

    train(args, loader, generator, discriminator, student_generator, student_discriminator, g_optim, d_optim, g_ema, student_g_ema, device)
