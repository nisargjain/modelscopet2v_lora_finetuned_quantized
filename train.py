import argparse
import datetime
import logging
import inspect
import math
import os
import random
import gc
import copy

from typing import Dict, Optional, Tuple
from omegaconf import OmegaConf

import cv2
import torch
import torch.nn.functional as F
import torch.utils.checkpoint
import torchvision.transforms as T
import diffusers
import transformers

from torchvision import transforms
from tqdm.auto import tqdm

from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import set_seed

from models.unet_3d_condition import UNet3DConditionModel
from diffusers.models import AutoencoderKL
from diffusers import DPMSolverMultistepScheduler, DDPMScheduler, TextToVideoSDPipeline
from diffusers.optimization import get_scheduler
from diffusers.utils import check_min_version, export_to_video
from diffusers.utils.import_utils import is_xformers_available
from diffusers.models.attention_processor import AttnProcessor2_0, Attention
from diffusers.models.attention import BasicTransformerBlock

from transformers import CLIPTextModel, CLIPTokenizer
from transformers.models.clip.modeling_clip import CLIPEncoder
from utils.dataset import VideoJsonDataset, SingleVideoDataset, \
    ImageDataset, VideoFolderDataset, CachedDataset
from einops import rearrange, repeat
from utils.lora_handler import LoraHandler, LORA_VERSIONS

already_printed_trainables = False

logger = get_logger(__name__, log_level="INFO")

def create_logging(logging, logger, accelerator):
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    logger.info(accelerator.state, main_process_only=False)

def accelerate_set_verbose(accelerator):
    if accelerator.is_local_main_process:
        transformers.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        transformers.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

def get_train_dataset(dataset_types, train_data, tokenizer):
    train_datasets = []

    # Loop through all available datasets, get the name, then add to list of data to process.
    for DataSet in [VideoJsonDataset, SingleVideoDataset, ImageDataset, VideoFolderDataset]:
        for dataset in dataset_types:
            if dataset == DataSet.__getname__():
                train_datasets.append(DataSet(**train_data, tokenizer=tokenizer))

    if len(train_datasets) > 0:
        return train_datasets
    else:
        raise ValueError("Dataset type not found: 'json', 'single_video', 'folder', 'image'")

def extend_datasets(datasets, dataset_items, extend=False):
    biggest_data_len = max(x.__len__() for x in datasets)
    extended = []
    for dataset in datasets:
        if dataset.__len__() == 0:
            del dataset
            continue
        if dataset.__len__() < biggest_data_len:
            for item in dataset_items:
                if extend and item not in extended and hasattr(dataset, item):
                    print(f"Extending {item}")

                    value = getattr(dataset, item)
                    value *= biggest_data_len
                    value = value[:biggest_data_len]

                    setattr(dataset, item, value)

                    print(f"New {item} dataset length: {dataset.__len__()}")
                    extended.append(item)

# def export_to_video(video_frames, output_video_path, fps):
#     if len(video_frames.shape) == 5:
#         k = len(video_frames)
#         fourcc = cv2.VideoWriter_fourcc(*"mp4v")
#         for jk in range(k):
#             temp_video_frame = video_frames[jk]
#             h, w, _ = temp_video_frame[0].shape
#             video_writer = cv2.VideoWriter(output_video_path, fourcc, fps=fps, frameSize=(w, h))
#             for i in range(len(temp_video_frame)):
#                 img = cv2.cvtColor(temp_video_frame[i], cv2.COLOR_RGB2BGR)
#                 if img.dtype != 'uint8':
#                     img = (img * 255).astype('uint8')  # Scale to 0-255 if it's normalized
#                 video_writer.write(img)
#     else:
#         fourcc = cv2.VideoWriter_fourcc(*"mp4v")
#         h, w, _ = video_frames[0].shape
#         video_writer = cv2.VideoWriter(output_video_path, fourcc, fps=fps, frameSize=(w, h))
#         for i in range(len(video_frames)):
#             img = cv2.cvtColor(video_frames[i], cv2.COLOR_RGB2BGR)
#             video_writer.write(img)
def export_to_video(video_frames, output_video_path, fps):
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    h, w, _ = video_frames[0].shape
    video_writer = cv2.VideoWriter(output_video_path, fourcc, fps=fps, frameSize=(w, h))
    for i in range(len(video_frames)):
        img = cv2.cvtColor(video_frames[i], cv2.COLOR_RGB2BGR)
        video_writer.write(img)

def create_output_folders(output_dir, config):
    now = datetime.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = os.path.join(output_dir, f"train_{now}")
    
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(f"{out_dir}/samples", exist_ok=True)
    OmegaConf.save(config, os.path.join(out_dir, 'config.yaml'))

    return out_dir

def load_primary_models(pretrained_model_path):
    noise_scheduler = DDPMScheduler.from_pretrained(pretrained_model_path, subfolder="scheduler")
    tokenizer = CLIPTokenizer.from_pretrained(pretrained_model_path, subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(pretrained_model_path, subfolder="text_encoder",local_files_only = True, use_safetensors=True)
    vae = AutoencoderKL.from_pretrained(pretrained_model_path, subfolder="vae")
    unet = UNet3DConditionModel.from_pretrained(pretrained_model_path, subfolder="unet",local_files_only = True, use_safetensors=True)

    return noise_scheduler, tokenizer, text_encoder, vae, unet

def unet_and_text_g_c(unet, text_encoder, unet_enable, text_enable):
    unet._set_gradient_checkpointing(value=unet_enable)
    text_encoder._set_gradient_checkpointing(CLIPEncoder)

def freeze_models(models_to_freeze):
    for model in models_to_freeze:
        if model is not None: model.requires_grad_(False) 
            
def is_attn(name):
   return ('attn1' or 'attn2' == name.split('.')[-1])

def set_processors(attentions):
    for attn in attentions: attn.set_processor(AttnProcessor2_0()) 

def set_torch_2_attn(unet):
    optim_count = 0
    
    for name, module in unet.named_modules():
        if is_attn(name):
            if isinstance(module, torch.nn.ModuleList):
                for m in module:
                    if isinstance(m, BasicTransformerBlock):
                        set_processors([m.attn1, m.attn2])
                        optim_count += 1
    if optim_count > 0: 
        print(f"{optim_count} Attention layers using Scaled Dot Product Attention.")

def handle_memory_attention(enable_xformers_memory_efficient_attention, enable_torch_2_attn, unet): 
    try:
        is_torch_2 = hasattr(F, 'scaled_dot_product_attention')
        enable_torch_2 = is_torch_2 and enable_torch_2_attn
        
        if enable_xformers_memory_efficient_attention and not enable_torch_2:
            if is_xformers_available():
                from xformers.ops import MemoryEfficientAttentionFlashAttentionOp
                unet.enable_xformers_memory_efficient_attention(attention_op=MemoryEfficientAttentionFlashAttentionOp)
            else:
                raise ValueError("xformers is not available. Make sure it is installed correctly")
        
        if enable_torch_2:
            set_torch_2_attn(unet)
            
    except:
        print("Could not enable memory efficient attention for xformers or Torch 2.0.")

def param_optim(model, condition, extra_params=None, is_lora=False, negation=None):
    extra_params = extra_params if len(extra_params.keys()) > 0 else None
    return {
        "model": model, 
        "condition": condition, 
        'extra_params': extra_params,
        'is_lora': is_lora,
        "negation": negation
    }
    

def create_optim_params(name='param', params=None, lr=5e-6, extra_params=None):
    params = {
        "name": name, 
        "params": params, 
        "lr": lr
    }
    if extra_params is not None:
        for k, v in extra_params.items():
            params[k] = v
    
    return params

def negate_params(name, negation):
    # We have to do this if we are co-training with LoRA.
    # This ensures that parameter groups aren't duplicated.
    if negation is None: return False
    for n in negation:
        if n in name and 'temp' not in name:
            return True
    return False


def create_optimizer_params(model_list, lr):
    import itertools
    optimizer_params = []

    for optim in model_list:
        model, condition, extra_params, is_lora, negation = optim.values()
        # Check if we are doing LoRA training.
        if is_lora and condition and isinstance(model, list): 
            params = create_optim_params(
                params=itertools.chain(*model), 
                extra_params=extra_params
            )
            optimizer_params.append(params)
            continue
            
        if is_lora and  condition and not isinstance(model, list):
            for n, p in model.named_parameters():
                if 'lora' in n:
                    params = create_optim_params(n, p, lr, extra_params)
                    optimizer_params.append(params)
            continue

        # If this is true, we can train it.
        if condition:
            for n, p in model.named_parameters():
                should_negate = 'lora' in n and not is_lora
                if should_negate: continue

                params = create_optim_params(n, p, lr, extra_params)
                optimizer_params.append(params)
    
    return optimizer_params

def get_optimizer(use_8bit_adam):
    if use_8bit_adam:
        try:
            import bitsandbytes as bnb
        except ImportError:
            raise ImportError(
                "Please install bitsandbytes to use 8-bit Adam. You can do so by running `pip install bitsandbytes`"
            )

        return bnb.optim.AdamW8bit
    else:
        return torch.optim.AdamW

def is_mixed_precision(accelerator):
    weight_dtype = torch.float32

    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16

    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16

    return weight_dtype

def cast_to_gpu_and_type(model_list, accelerator, weight_dtype):
    for model in model_list:
        if model is not None: model.to(accelerator.device, dtype=weight_dtype)

def handle_cache_latents(
        should_cache, 
        output_dir, 
        train_dataloader, 
        train_batch_size, 
        vae, 
        cached_latent_dir=None,
        shuffle=False
    ):

    # Cache latents by storing them in VRAM. 
    # Speeds up training and saves memory by not encoding during the train loop.
    if not should_cache: return None
    vae.to('cuda', dtype=torch.float16)
    vae.enable_slicing()
    
    cached_latent_dir = (
        os.path.abspath(cached_latent_dir) if cached_latent_dir is not None else None 
        )

    if cached_latent_dir is None:
        cache_save_dir = f"{output_dir}/cached_latents"
        os.makedirs(cache_save_dir, exist_ok=True)

        for i, batch in enumerate(tqdm(train_dataloader, desc="Caching Latents.")):

            save_name = f"cached_{i}"
            full_out_path =  f"{cache_save_dir}/{save_name}.pt"

            pixel_values = batch['pixel_values'].to('cuda', dtype=torch.float16)
            batch['pixel_values'] = tensor_to_vae_latent(pixel_values, vae)
            for k, v in batch.items(): batch[k] = v[0]
        
            torch.save(batch, full_out_path)
            del pixel_values
            del batch

            # We do this to avoid fragmentation from casting latents between devices.
            torch.cuda.empty_cache()
    else:
        cache_save_dir = cached_latent_dir
        

    return torch.utils.data.DataLoader(
        CachedDataset(cache_dir=cache_save_dir), 
        batch_size=train_batch_size, 
        shuffle=shuffle,
        num_workers=0
    ) 

def handle_trainable_modules(model, trainable_modules=None, is_enabled=True, negation=None):
    global already_printed_trainables
    acc = []
    unfrozen_params = 0
    countparams = 0
    for name, param in model.named_parameters():
        # print("name: ", name)
        countparams += param.numel()
    print("Total Weights: ", countparams)
    countparams = 0
    if trainable_modules is not None:
        unlock_all = any([name == 'all' for name in trainable_modules])
        if unlock_all:
            model.requires_grad_(True)
            unfrozen_params = len(list(model.parameters()))
            countparams = sum(p.numel() for p in model.parameters())
        else:
            model.requires_grad_(False)
            for name, param in model.named_parameters():
                for tm in trainable_modules:
                    if all([tm in name, name not in acc, 'lora' not in name]):
                        param.requires_grad_(is_enabled)
                        acc.append(name)
                        unfrozen_params += 1
                        countparams += param.numel()
                        
    if unfrozen_params > 0 and not already_printed_trainables:
        already_printed_trainables = True 
        print(f"{unfrozen_params} params have been processed.")
        print("Total weights trainable: ", countparams)
def tensor_to_vae_latent(t, vae):
    video_length = t.shape[1]

    t = rearrange(t, "b f c h w -> (b f) c h w")
    latents = vae.encode(t).latent_dist.sample()
    latents = rearrange(latents, "(b f) c h w -> b c f h w", f=video_length)
    latents = latents * 0.18215

    return latents

def sample_noise(latents, noise_strength, use_offset_noise=False):
    b ,c, f, *_ = latents.shape
    noise_latents = torch.randn_like(latents, device=latents.device)
    offset_noise = None

    if use_offset_noise:
        offset_noise = torch.randn(b, c, f, 1, 1, device=latents.device)
        noise_latents = noise_latents + noise_strength * offset_noise

    return noise_latents

def enforce_zero_terminal_snr(betas):
    """
    Corrects noise in diffusion schedulers.
    From: Common Diffusion Noise Schedules and Sample Steps are Flawed
    https://arxiv.org/pdf/2305.08891.pdf
    """
    # Convert betas to alphas_bar_sqrt
    alphas = 1 - betas
    alphas_bar = alphas.cumprod(0)
    alphas_bar_sqrt = alphas_bar.sqrt()

    # Store old values.
    alphas_bar_sqrt_0 = alphas_bar_sqrt[0].clone()
    alphas_bar_sqrt_T = alphas_bar_sqrt[-1].clone()

    # Shift so the last timestep is zero.
    alphas_bar_sqrt -= alphas_bar_sqrt_T

    # Scale so the first timestep is back to the old value.
    alphas_bar_sqrt *= alphas_bar_sqrt_0 / (
        alphas_bar_sqrt_0 - alphas_bar_sqrt_T
    )

    # Convert alphas_bar_sqrt to betas
    alphas_bar = alphas_bar_sqrt ** 2
    alphas = alphas_bar[1:] / alphas_bar[:-1]
    alphas = torch.cat([alphas_bar[0:1], alphas])
    betas = 1 - alphas

    return betas

def should_sample(global_step, validation_steps, validation_data):
    return (global_step % validation_steps == 0 or global_step == 1)  \
    and validation_data.sample_preview

def save_pipe(
        path, 
        global_step,
        accelerator, 
        unet, 
        text_encoder, 
        vae, 
        output_dir,
        lora_manager: LoraHandler,
        unet_target_replace_module=None,
        text_target_replace_module=None,
        is_checkpoint=False,
        save_pretrained_model=True
    ):

    if is_checkpoint:
        save_path = os.path.join(output_dir, f"checkpoint-{global_step}")
        os.makedirs(save_path, exist_ok=True)
    else:
        save_path = output_dir

    # Save the dtypes so we can continue training at the same precision.
    u_dtype, t_dtype, v_dtype = unet.dtype, text_encoder.dtype, vae.dtype 

   # Copy the model without creating a reference to it. This allows keeping the state of our lora training if enabled.
    unet_save = copy.deepcopy(unet.cpu())
    text_encoder_save = copy.deepcopy(text_encoder.cpu())

    unet_out = copy.deepcopy(accelerator.unwrap_model(unet_save, keep_fp32_wrapper=False))
    text_encoder_out = copy.deepcopy(accelerator.unwrap_model(text_encoder_save, keep_fp32_wrapper=False))

    pipeline = TextToVideoSDPipeline.from_pretrained(
        path,
        unet=unet_out,
        text_encoder=text_encoder_out,
        vae=vae,
    ).to(torch_dtype=torch.float32)
    
    lora_manager.save_lora_weights(model=pipeline, save_path=save_path, step=global_step)

    if save_pretrained_model:
        pipeline.save_pretrained(save_path)

    if is_checkpoint:
        unet, text_encoder = accelerator.prepare(unet, text_encoder)
        models_to_cast_back = [(unet, u_dtype), (text_encoder, t_dtype), (vae, v_dtype)]
        [x[0].to(accelerator.device, dtype=x[1]) for x in models_to_cast_back]

    logger.info(f"Saved model at {save_path} on step {global_step}")
    
    del pipeline
    del unet_out
    del text_encoder_out
    torch.cuda.empty_cache()
    gc.collect()


def replace_prompt(prompt, token, wlist):
    for w in wlist:
        if w in prompt: return prompt.replace(w, token)
    return prompt 

def main(
    pretrained_model_path: str,
    output_dir: str,
    train_data: Dict,
    validation_data: Dict,
    extra_train_data: list = [],
    dataset_types: Tuple[str] = ('json'),
    shuffle: bool = True,
    validation_steps: int = 100,
    trainable_modules: Tuple[str] = None, # Eg: ("attn1", "attn2")
    trainable_text_modules: Tuple[str] = None, # Eg: ("all"), this also applies to trainable_modules
    extra_unet_params = None,
    extra_text_encoder_params = None,
    train_batch_size: int = 1,
    max_train_steps: int = 500,
    learning_rate: float = 5e-5,
    scale_lr: bool = False,
    lr_scheduler: str = "constant",
    lr_warmup_steps: int = 0,
    adam_beta1: float = 0.9,
    adam_beta2: float = 0.999,
    adam_weight_decay: float = 1e-2,
    adam_epsilon: float = 1e-08,
    max_grad_norm: float = 1.0,
    gradient_accumulation_steps: int = 1,
    gradient_checkpointing: bool = False,
    text_encoder_gradient_checkpointing: bool = False,
    checkpointing_steps: int = 500,
    resume_from_checkpoint: Optional[str] = None,
    resume_step: Optional[int] = None,
    mixed_precision: Optional[str] = "fp16",
    use_8bit_adam: bool = False,
    enable_xformers_memory_efficient_attention: bool = True,
    enable_torch_2_attn: bool = False,
    seed: Optional[int] = None,
    train_text_encoder: bool = False,
    use_offset_noise: bool = False,
    rescale_schedule: bool = False,
    offset_noise_strength: float = 0.1,
    extend_dataset: bool = False,
    cache_latents: bool = False,
    cached_latent_dir = None,
    lora_version: LORA_VERSIONS = LORA_VERSIONS[0],
    save_lora_for_webui: bool = False,
    only_lora_for_webui: bool = False,
    lora_bias: str = 'none',
    use_unet_lora: bool = False,
    use_text_lora: bool = False,
    unet_lora_modules: Tuple[str] = ["ResnetBlock2D"],
    text_encoder_lora_modules: Tuple[str] = ["CLIPEncoderLayer"],
    save_pretrained_model: bool = True,
    lora_rank: int = 16,
    lora_path: str = '',
    lora_unet_dropout: float = 0.1,
    lora_text_dropout: float = 0.1,
    logger_type: str = 'tensorboard',
    **kwargs
):

    *_, config = inspect.getargvalues(inspect.currentframe())

    accelerator = Accelerator(
        gradient_accumulation_steps=gradient_accumulation_steps,
        mixed_precision=mixed_precision,
        log_with=logger_type,
        project_dir=output_dir
    )

    # Make one log on every process with the configuration for debugging.
    create_logging(logging, logger, accelerator)
    logging.getLogger("models.unet_3d_condition").setLevel(logging.WARNING)
    # Initialize accelerate, transformers, and diffusers warnings
    accelerate_set_verbose(accelerator)

    # If passed along, set the training seed now.
    if seed is not None:
        set_seed(seed)

    # Handle the output folder creation
    if accelerator.is_main_process:
       output_dir = create_output_folders(output_dir, config)

    # Load scheduler, tokenizer and models.
    noise_scheduler, tokenizer, text_encoder, vae, unet = load_primary_models(pretrained_model_path)

    # Freeze any necessary models
    freeze_models([vae, text_encoder, unet])
    
    # Enable xformers if available
    handle_memory_attention(enable_xformers_memory_efficient_attention, enable_torch_2_attn, unet)

    if scale_lr:
        learning_rate = (
            learning_rate * gradient_accumulation_steps * train_batch_size * accelerator.num_processes
        )

    # Initialize the optimizer
    optimizer_cls = get_optimizer(use_8bit_adam)

    # Use LoRA if enabled.  
    lora_manager = LoraHandler(
        version=lora_version, 
        use_unet_lora=use_unet_lora,
        use_text_lora=use_text_lora,
        save_for_webui=save_lora_for_webui,
        only_for_webui=only_lora_for_webui,
        unet_replace_modules=unet_lora_modules,
        text_encoder_replace_modules=text_encoder_lora_modules,
        lora_bias=lora_bias
    )

    unet_lora_params, unet_negation = lora_manager.add_lora_to_model(
        use_unet_lora, unet, lora_manager.unet_replace_modules, lora_unet_dropout, lora_path, r=lora_rank) 

    text_encoder_lora_params, text_encoder_negation = lora_manager.add_lora_to_model(
        use_text_lora, text_encoder, lora_manager.text_encoder_replace_modules, lora_text_dropout, lora_path, r=lora_rank) 

    # Create parameters to optimize over with a condition (if "condition" is true, optimize it)
    extra_unet_params = extra_unet_params if extra_unet_params is not None else {}
    extra_text_encoder_params = extra_unet_params if extra_unet_params is not None else {}

    trainable_modules_available = trainable_modules is not None
    trainable_text_modules_available = (train_text_encoder and trainable_text_modules is not None)
    
    optim_params = [
        param_optim(unet, trainable_modules_available, extra_params=extra_unet_params, negation=unet_negation),
        param_optim(text_encoder, trainable_text_modules_available, 
                        extra_params=extra_text_encoder_params, 
                        negation=text_encoder_negation
                   ),
        param_optim(text_encoder_lora_params, use_text_lora, is_lora=True, 
                        extra_params={**{"lr": learning_rate}, **extra_text_encoder_params}
                    ),
        param_optim(unet_lora_params, use_unet_lora, is_lora=True, 
                        extra_params={**{"lr": learning_rate}, **extra_unet_params}
                    )
    ]

    params = create_optimizer_params(optim_params, learning_rate)
    
    # Create Optimizer
    optimizer = optimizer_cls(
        params,
        lr=learning_rate,
        betas=(adam_beta1, adam_beta2),
        weight_decay=adam_weight_decay,
        eps=adam_epsilon,
    )

    # Scheduler
    lr_scheduler = get_scheduler(
        lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=lr_warmup_steps * gradient_accumulation_steps,
        num_training_steps=max_train_steps * gradient_accumulation_steps,
    )

    # Get the training dataset based on types (json, single_video, image)
    train_datasets = get_train_dataset(dataset_types, train_data, tokenizer)

    # If you have extra train data, you can add a list of however many you would like.
    # Eg: extra_train_data: [{: {dataset_types, train_data: {etc...}}}] 
    try:
        if extra_train_data is not None and len(extra_train_data) > 0:
            for dataset in extra_train_data:
                d_t, t_d = dataset['dataset_types'], dataset['train_data']
                train_datasets += get_train_dataset(d_t, t_d, tokenizer)

    except Exception as e:
        print(f"Could not process extra train datasets due to an error : {e}")

    # Extend datasets that are less than the greatest one. This allows for more balanced training.
    attrs = ['train_data', 'frames', 'image_dir', 'video_files']
    extend_datasets(train_datasets, attrs, extend=extend_dataset)

    # Process one dataset
    if len(train_datasets) == 1:
        train_dataset = train_datasets[0]
    
    # Process many datasets
    else:
        train_dataset = torch.utils.data.ConcatDataset(train_datasets) 

    # DataLoaders creation:
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset, 
        batch_size=train_batch_size,
        shuffle=shuffle
    )

     # Latents caching
    cached_data_loader = handle_cache_latents(
        cache_latents, 
        output_dir,
        train_dataloader, 
        train_batch_size, 
        vae,
        cached_latent_dir
    ) 

    if cached_data_loader is not None: 
        train_dataloader = cached_data_loader

    # Prepare everything with our `accelerator`.
    unet, optimizer,train_dataloader, lr_scheduler, text_encoder = accelerator.prepare(
        unet, 
        optimizer, 
        train_dataloader, 
        lr_scheduler, 
        text_encoder
    )

    # Use Gradient Checkpointing if enabled.
    unet_and_text_g_c(
        unet, 
        text_encoder, 
        gradient_checkpointing, 
        text_encoder_gradient_checkpointing
    )
    
    # Enable VAE slicing to save memory.
    vae.enable_slicing()

    # For mixed precision training we cast the text_encoder and vae weights to half-precision
    # as these models are only used for inference, keeping weights in full precision is not required.
    weight_dtype = is_mixed_precision(accelerator)

    # Move text encoders, and VAE to GPU
    models_to_cast = [text_encoder, vae]
    cast_to_gpu_and_type(models_to_cast, accelerator, weight_dtype)

    # Fix noise schedules to predcit light and dark areas if available.
    if not use_offset_noise and rescale_schedule:
        noise_scheduler.betas = enforce_zero_terminal_snr(noise_scheduler.betas)

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / gradient_accumulation_steps)

    # Afterwards we recalculate our number of training epochs
    num_train_epochs = math.ceil(max_train_steps / num_update_steps_per_epoch)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        accelerator.init_trackers("text2video-fine-tune")

    # Train!
    total_batch_size = train_batch_size * accelerator.num_processes * gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {max_train_steps}")
    global_step = 0
    first_epoch = 0

    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(global_step, max_train_steps), disable=not accelerator.is_local_main_process)
    progress_bar.set_description("Steps")

    def finetune_unet(batch, train_encoder=False):
        nonlocal use_offset_noise
        nonlocal rescale_schedule
        
        # Check if we are training the text encoder
        text_trainable = (train_text_encoder or lora_manager.use_text_lora)
        
        # Unfreeze UNET Layers
        if global_step == 0: 
            already_printed_trainables = False
            unet.train()
            handle_trainable_modules(
                unet, 
                trainable_modules, 
                is_enabled=True,
                negation=unet_negation
            )

        # Convert videos to latent space
        pixel_values = batch["pixel_values"]

        if not cache_latents:
            latents = tensor_to_vae_latent(pixel_values, vae)
        else:
            latents = pixel_values

        # Get video length
        video_length = latents.shape[2]

        # Sample noise that we'll add to the latents
        use_offset_noise = use_offset_noise and not rescale_schedule
        noise = sample_noise(latents, offset_noise_strength, use_offset_noise)
        bsz = latents.shape[0]

        # Sample a random timestep for each video
        timesteps = torch.randint(0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device)
        timesteps = timesteps.long()

        # Add noise to the latents according to the noise magnitude at each timestep
        # (this is the forward diffusion process)
        noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps)
    
        # Enable text encoder training
        if text_trainable:
            text_encoder.train()

            if lora_manager.use_text_lora: 
                text_encoder.text_model.embeddings.requires_grad_(True)

            if global_step == 0 and train_text_encoder:
                handle_trainable_modules(
                    text_encoder, 
                    trainable_modules=trainable_text_modules,
                    negation=text_encoder_negation
            )
            cast_to_gpu_and_type([text_encoder], accelerator, torch.float32)
               
        # *Potentially* Fixes gradient checkpointing training.
        # See: https://github.com/prigoyal/pytorch_memonger/blob/master/tutorial/Checkpointing_for_PyTorch_models.ipynb

        if kwargs.get('eval_train', False):
            unet.eval()
            text_encoder.eval()

        # Encode text embeddings
        token_ids = batch['prompt_ids']

        # Assume extra batch dimnesion.
        if len(token_ids.shape) > 2:
            token_ids = token_ids[0]
            
        encoder_hidden_states = text_encoder(token_ids)[0]

        # Get the target for loss depending on the prediction type
        if noise_scheduler.config.prediction_type == "epsilon":
            target = noise

        elif noise_scheduler.config.prediction_type == "v_prediction":
            target = noise_scheduler.get_velocity(latents, noise, timesteps)

        else:
            raise ValueError(f"Unknown prediction type {noise_scheduler.config.prediction_type}")

        
        # Here we do two passes for video and text training.
        # If we are on the second iteration of the loop, get one frame.
        # This allows us to train text information only on the spatial layers.
        losses = []
        should_truncate_video = (video_length > 1 and text_trainable)

        # We detach the encoder hidden states for the first pass (video frames > 1)
        # Then we make a clone of the initial state to ensure we can train it in the loop.
        detached_encoder_state = encoder_hidden_states.clone().detach()
        trainable_encoder_state = encoder_hidden_states.clone()

        for i in range(2):

            should_detach = noisy_latents.shape[2] > 1 and i == 0

            if should_truncate_video and i == 1:
                noisy_latents = noisy_latents[:,:,1,:,:].unsqueeze(2)
                target = target[:,:,1,:,:].unsqueeze(2)
                       
            encoder_hidden_states = (
                detached_encoder_state if should_detach else trainable_encoder_state
            )

            model_pred = unet(noisy_latents, timesteps, encoder_hidden_states=encoder_hidden_states).sample
            loss = F.mse_loss(model_pred.float(), target.float(), reduction="mean")

            losses.append(loss)
            
            # This was most likely single frame training or a single image.
            if video_length == 1 and i == 0: break

        loss = losses[0] if len(losses) == 1 else losses[0] + losses[1] 

        return loss, latents

    for epoch in range(first_epoch, num_train_epochs):
        train_loss = 0.0
        # print("epoch no: ", epoch)
        for step, batch in enumerate(train_dataloader):
            # print('Step no: ', step)
            # Skip steps until we reach the resumed step
            if resume_from_checkpoint and epoch == first_epoch and step < resume_step:
                if step % gradient_accumulation_steps == 0:
                    progress_bar.update(1)
                continue


            with accelerator.accumulate(unet) ,accelerator.accumulate(text_encoder):

                text_prompt = batch['text_prompt'][0]
                
                with accelerator.autocast():
                    loss, latents = finetune_unet(batch, train_encoder=train_text_encoder)
                # Gather the losses across all processes for logging (if we use distributed training).
                avg_loss = accelerator.gather(loss.repeat(train_batch_size)).mean()
                train_loss += avg_loss.item() / gradient_accumulation_steps

                # Backpropagate
                try:
                    accelerator.backward(loss)
                    if any([train_text_encoder, use_text_lora]):
                        params_to_clip = list(unet.parameters()) + list(text_encoder.parameters())
                    else:
                        params_to_clip = unet.parameters()

                    if max_grad_norm > 0:
                        if accelerator.sync_gradients:
                            if any([train_text_encoder, use_text_lora]):
                                params_to_clip = list(unet.parameters()) + list(text_encoder.parameters())
                            else:
                                params_to_clip = list(unet.parameters())
                                
                            accelerator.clip_grad_norm_(params_to_clip, max_grad_norm)
                            
                    optimizer.step()
                    lr_scheduler.step()
                    optimizer.zero_grad(set_to_none=True)
                    
                except Exception as e:
                    print(f"An error has occured during backpropogation! {e}") 
                    continue

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                global_step += 1
                accelerator.log({"train_loss": train_loss}, step=global_step)
                train_loss = 0.0
            
                if global_step % checkpointing_steps == 0:
                    save_pipe(
                        pretrained_model_path, 
                        global_step, 
                        accelerator, 
                        unet, 
                        text_encoder, 
                        vae, 
                        output_dir, 
                        lora_manager,
                        unet_lora_modules,
                        text_encoder_lora_modules,
                        is_checkpoint=True,
                        save_pretrained_model=save_pretrained_model
                    )

                if should_sample(global_step, validation_steps, validation_data):
                    if global_step == 1: print("Performing validation prompt.")
                    if accelerator.is_main_process:
                        
                        with accelerator.autocast():
                            unet.eval()
                            text_encoder.eval()
                            unet_and_text_g_c(unet, text_encoder, False, False)
                            lora_manager.deactivate_lora_train([unet, text_encoder], True)    

                            pipeline = TextToVideoSDPipeline.from_pretrained(
                                pretrained_model_path,
                                text_encoder=text_encoder,
                                vae=vae,
                                unet=unet
                            )

                            diffusion_scheduler = DPMSolverMultistepScheduler.from_config(pipeline.scheduler.config)
                            pipeline.scheduler = diffusion_scheduler

                            prompt = text_prompt if len(validation_data.prompt) <= 0 else validation_data.prompt

                            curr_dataset_name = batch['dataset']
                            save_filename = f"{prompt}_epoch{epoch}_{global_step}"

                            out_file = f"{output_dir}/samples/{save_filename}.mp4"
                            
                            with torch.no_grad():
                                video_frames = pipeline(
                                    prompt,
                                    width=validation_data.width,
                                    height=validation_data.height,
                                    num_frames=validation_data.num_frames,
                                    num_inference_steps=validation_data.num_inference_steps,
                                    guidance_scale=validation_data.guidance_scale
                                ).frames
                            export_to_video(video_frames, out_file, train_data.get('fps', 8))

                            del pipeline
                            torch.cuda.empty_cache()

                    logger.info(f"Saved a new sample to {out_file}")

                    unet_and_text_g_c(
                        unet, 
                        text_encoder, 
                        gradient_checkpointing, 
                        text_encoder_gradient_checkpointing
                    )

                    lora_manager.deactivate_lora_train([unet, text_encoder], False)    

            logs = {"step_loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0]}
            accelerator.log({"training_loss": loss.detach().item()}, step=step)
            progress_bar.set_postfix(**logs)

            if global_step >= max_train_steps:
                break

    # Create the pipeline using the trained modules and save it.
    accelerator.wait_for_everyone()
    if accelerator.is_main_process:
        save_pipe(
                pretrained_model_path, 
                global_step, 
                accelerator, 
                unet, 
                text_encoder, 
                vae, 
                output_dir, 
                lora_manager,
                unet_lora_modules,
                text_encoder_lora_modules,
                is_checkpoint=False,
                save_pretrained_model=save_pretrained_model
        )     
    accelerator.end_training()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="./configs/my_config.yaml")
    args = parser.parse_args()

    main(**OmegaConf.load(args.config))
