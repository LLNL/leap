################################################################################
# Copyright 2022-2023 Lawrence Livermore National Security, LLC and other 
# LEAP project developers. See the LICENSE file for details.
# SPDX-License-Identifier: MIT
#
# LivermorE AI Projector for Computed Tomography (LEAP)
# demo: test reconstruction example using projector class
# fast iterative shrinkage threshold algorithm (FISTA) and total variation (TV)-based method:
# A. Beck and M. Teboulle, "Fast Gradient-Based Algorithms for Constrained Total Variation Image Denoising 
# and Deblurring Problems," in IEEE Transactions on Image Processing, vol. 18, no. 11, pp. 2419-2434, Nov. 2009
################################################################################

# example:
# param_fn=/p/vast1/mlct/CT_COE_Imatron/param_parallel512.cfg
# data_dir=/usr/workspace/kim63/src/ctnetplus_techmat/results/20230222_la512/test/
# python test_recon_TV.py --param-fn ${param_fn}  --init-fn ${data_dir}/S_193_0100_pred.npy  --proj-fn ${data_dir}/S_193_0100_sino.npy --mask-fn ${data_dir}/S_193_0100_mask.npy
# python test_recon_TV.py --param-fn ${param_fn}  --proj-fn ${data_dir}/S_193_0100_sino3.npy

import os
import sys
sys.stdout.flush()
import argparse
import numpy as np
import imageio

import torch
import torch.nn as nn
from torch.optim import Adam, SGD, Adagrad, lr_scheduler

from leaptorch import Projector
from TVGPUClass import TVGPUClass


# program arguments
parser = argparse.ArgumentParser()
parser.add_argument("--init-fn", default="", help="path to image file for initial guess (image prior)")
parser.add_argument("--proj-fn", default="sample_data/FORBILD_head_64_sino.npy", help="path to input projection data file")
parser.add_argument("--mask-fn", default="", help="path to input projection mask file")
parser.add_argument("--param-fn", default="sample_data/param_parallel64.cfg", help="path to projection geometry configuration file")
parser.add_argument("--output-dir", default="sample_data", help="directory storing intermediate files")
parser.add_argument("--use-fov", action='store_true', default=False, help="whether fov is used or not")
args = parser.parse_args()



def create_circular_mask(h, w, center=None, radius=None):
    if center is None:
        center = (int(h/2)-0.5, int(w/2)-0.5)
    if radius is None:
        radius = min(center[0], center[1], w-center[0], h-center[1])

    y, x = np.ogrid[:h, :w]
    dist_from_center = np.sqrt((x - center[0])**2 + (y-center[1])**2)

    mask = dist_from_center <= radius
    return mask


# CT reconstruction solver 
class Reconstructor:
    """
    Accelerated Proximal Gradient Descent with TV
    """
    def __init__(self, projector, device_name, learning_rate=1., use_decay=False,
                 iter_count=2000, stop_criterion=1e-1, save_dir='.', save_freq=10, verbose=1):

        # set nn_model and projector
        self.projector = projector
        self.device_name = device_name

        # set up hyperparameters
        self.learning_rate = learning_rate
        self.use_decay = use_decay
        self.iter_count = iter_count
        self.stop_criterion = stop_criterion
        self.save_dir = save_dir
        self.save_freq = save_freq
        self.verbose = verbose
        # set up TV
        self.rObj = TVGPUClass(Lambda=1e-4)

    def loss_func(self, input, target):
        return ((input - target) ** 2).mean()
        
    def reconstruct(self, g, g_mask, f_init, f_fov=None, fn_prefix="output"):
        # if no neural network is used, make f trainable
        self.projector.train()
        
        x = f_init.clone() # x is the image vector update during iterartions
        s = x.clone() # acceleration vector 
        p = torch.zeros([2,  x.shape[-2], x.shape[-1]], dtype=torch.float32) # dual variable for TV
        t = torch.tensor([1.]).float().to(device) # 
        accelerate, clip, alpha = True, True, 1.
 
        # main optimization iteration
        for i in range(self.iter_count):

            # compute loss
            s.requires_grad = True
            g_pred = self.projector(s).cpu().float()
            if g_mask != None:
                g_pred_ = g_pred * g_mask.cpu().float()
                g_pred = g_pred_
            loss = self.loss_func(g_pred, g.cpu().float())
            grad = torch.autograd.grad(loss, s, retain_graph=True, create_graph=True)[0]

            with torch.no_grad():
                vnext = s-self.learning_rate*grad
                Px,p = self.rObj.prox(vnext.squeeze(), self.learning_rate, p.squeeze())#rObj.prox(vnext.squeeze(), gamma, p.squeeze())   # clip to [0, inf]
                xnext = Px[None,None,...]
                xnext = (1-alpha)*xnext + alpha*vnext

                if clip:
                    xnext[xnext<=0] = 0

                # acceleration
                if accelerate:
                    tnext = 0.5*(1+torch.sqrt(1+4*t*t))
                else:
                    tnext = 1
                s = xnext + ((t-1)/tnext)*(xnext-x)
                
                # update
                t = tnext
                x = xnext
        
            if i == 0:
                self.firstLoss = loss.cpu().data.item()

            # status display and save images
            loss_val = loss.cpu().data.item()
            if self.verbose > 0:
                print("[%d/%d] %s training loss %.9f , grad_norm %.9f, img_max %.4f" % (i, self.iter_count, self.device_name, loss_val/self.firstLoss, grad.norm(), x.max()))
            if loss_val/self.firstLoss < self.stop_criterion:
                break
            if i % self.save_freq == 0:
                f_img = x.cpu().detach().numpy()[0,0,:,:]
                if np.max(f_img) == 0:
                    scaleVal = 1
                else:
                    scaleVal = 255.0/np.max(f_img)
                #imageio.imsave(os.path.join(self.save_dir, "%s_LEAP_%s_%07d.png" % (fn_prefix, self.device_name, i)), scaleVal*f_img)

        # eval mode to get final f
        if f_fov != None:
            x = x * f_fov
        f_img = x.cpu().detach().numpy()[0,0,:,:]
        if np.max(f_img) == 0:
            scaleVal = 1
        else:
            scaleVal = 255.0/np.max(f_img)
        imageio.imsave(os.path.join(self.save_dir, "%s_LEAP_%s_final.png" % (fn_prefix, self.device_name)), scaleVal*f_img)
        return x


# if CUDA is available, use the first GPU
use_cuda = torch.cuda.is_available()
#use_cuda = False
if use_cuda:
    print("##### GPU CUDA mode #####")
    device_name = "cuda:0"
    device = torch.device(device_name)
    torch.cuda.set_device(0)    
else:
    print("##### CPU mode #####")
    device_name = "cpu"
    device = torch.device(device_name)


################################################################################
# 1. Read or simulate F and G using LEAP
################################################################################

# read arguments
output_dir = args.output_dir
init_fn = args.init_fn
proj_fn = args.proj_fn
mask_fn = args.mask_fn
param_fn = args.param_fn
use_fov = args.use_fov

# initialize projector and load parameters
proj = Projector(use_static=False, use_gpu=use_cuda, gpu_device=device, batch_size=1)
proj.load_param(param_fn)
proj.set_projector(1)
proj.print_param()

# load g and initialize f
g = np.load(proj_fn)
g = g.reshape((1, g.shape[0], 1, g.shape[1]))
g = torch.from_numpy(g)

if len(mask_fn) > 0:
    g_mask = np.load(mask_fn)
    g_mask = g_mask.reshape((1, g_mask.shape[0], 1, g_mask.shape[1]))
    g_mask = torch.from_numpy(g_mask)
else:
    g_mask = None

mout = torch.zeros_like(g)
#mout[0:720,...] = 1
mout[0:g.shape[0],...] = 1
g = mout*g.clone()

g = g[:,None,:]
# g = torch.stack((g,g,g,g), dim=0) ## modified to simulate batch_size=4
print("projection loaded: ", g.shape)

dimz, dimy, dimx = proj.get_volume_dim()
views, rows, cols = proj.get_projection_dim()
#print(dimz, dimy, dimx, views, rows, cols)
M = dimz
N = dimx

# load image prior if available
if len(init_fn) > 0:
    f_init = np.load(init_fn)
    f_init = f_init.reshape((1,1,f_init.shape[0],f_init.shape[1]))
else:
    # initialize f to be solved, given g above
    f_init = np.zeros((1, M, N, N)).astype(np.float32) ## modified by jiaming to simulate batch_size=4
f_init = torch.from_numpy(f_init).to(device)

# set mask for field of view
if args.use_fov:
    f_fov = create_circular_mask(N, N).reshape(1, M, N, N).astype(np.float32)
    f_fov = torch.from_numpy(f_fov)
    print("field of view masking is used")
else:
    f_fov = None
    print("no field of view masking is used")

# initialize and run reconstructor (solver)
if not os.path.exists(output_dir):
    os.makedirs(output_dir)
solver = Reconstructor(proj, device_name, learning_rate=0.01, use_decay=False, stop_criterion=1e-7, save_dir=output_dir)
f_final = solver.reconstruct(g, g_mask, f_init, f_fov, "f")

# save final reconstructed image
f_np = f_final[0,0,:,:].cpu().detach().numpy()
np.save(os.path.join(proj_fn[:-4]+"_TV.npy"), f_np)
imageio.imsave(os.path.join(proj_fn[:-4]+"_TV.png"), f_np/np.max(f_np)*255)
