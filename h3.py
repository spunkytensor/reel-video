# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Local MiniMax H3 text/image-to-video with native audio on a 32 GB NVIDIA GPU."""

import argparse
import fcntl
import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_ID = "MiniMaxAI/MiniMax-H3"
MODEL_REVISION = "42ed227ee7df40d41602854ae760620d6eb651fe"
MODEL_PATH = ROOT / "models" / MODEL_REVISION
COMPONENTS = (
    "transformer",
    "text_encoder",
    "tokenizer",
    "processor",
    "vae",
    "audio_vae",
    "scheduler",
    "audio_scheduler",
)


def frame_count(seconds: float) -> int:
    """Round up to the H3 VAE's frame grid without exceeding its duration limit."""
    if not math.isfinite(seconds) or not 5 <= seconds <= 15:
        raise ValueError("Duration must be between 5 and 15 seconds.")
    # The VAE accepts 17n + 5 frames. Round upward at 24 fps, so a requested
    # five seconds becomes 124 frames (about 5.17 seconds), not exactly 120.
    frames = math.ceil((math.ceil(seconds * 24) - 5) / 17) * 17 + 5
    if frames > 360:
        raise ValueError("The longest valid clip is 345 frames (14.375 seconds).")
    return frames


def validate_generation(args: argparse.Namespace) -> None:
    if not args.prompt.strip():
        raise ValueError("Prompt must not be empty.")
    generation_workflow(args)
    frame_count(args.seconds)
    if any(value < 32 or value % 32 for value in (args.width, args.height)):
        raise ValueError("Width and height must be positive multiples of 32.")
    if args.width * args.height > 1344 * 768:
        raise ValueError("This 32 GB profile supports at most 1344 × 768 pixels.")
    if not 0.25 <= args.width / args.height <= 4:
        raise ValueError("Aspect ratio must be between 1:4 and 4:1.")
    if args.steps < 2:
        raise ValueError("At least two sigma-grid points are required.")
    if not 0 <= args.seed < 2**63:
        raise ValueError("Seed must be between 0 and 2**63 - 1.")
    if args.output.suffix.lower() != ".mp4":
        raise ValueError("Output must have an .mp4 extension.")
    if args.output.exists() or args.output.with_suffix(".json").exists():
        raise ValueError(
            "Output or its metadata already exists; choose a new filename."
        )


def check_hardware() -> dict:
    import psutil
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is unavailable. Check the NVIDIA driver and PyTorch installation."
        )
    free, total = torch.cuda.mem_get_info()
    ram = psutil.virtual_memory()
    info = {
        "gpu": torch.cuda.get_device_name(),
        "cuda": torch.version.cuda,
        "torch": torch.__version__,
        "gpu_free_gib": round(free / 2**30, 2),
        "gpu_total_gib": round(total / 2**30, 2),
        "ram_available_gib": round(ram.available / 2**30, 2),
    }
    print(json.dumps(info, indent=2), flush=True)
    if free < 28 * 2**30:
        raise RuntimeError(
            "This profile needs 28 GiB free VRAM. Stop vLLM or other GPU workloads first."
        )
    if ram.available < 100 * 2**30:
        raise RuntimeError(
            "This profile needs at least 100 GiB available host RAM for loading/offload."
        )
    # Exercise a real CUDA kernel, not just device discovery.
    x = torch.ones((32, 32), device="cuda", dtype=torch.bfloat16)
    assert (x @ x).float().mean().item() == 32
    return info


def download(include_references=False) -> None:
    from huggingface_hub import snapshot_download

    components = (*COMPONENTS, "transformer_ref") if include_references else COMPONENTS
    snapshot_download(
        MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=MODEL_PATH,
        allow_patterns=[
            "LICENSE",
            "README.md",
            "model_index.json",
            "modular_model_index.json",
            *[f"{name}/*" for name in components],
        ],
        max_workers=4,
    )
    print(f"Downloaded pinned components {components} to {MODEL_PATH}", flush=True)


def generation_workflow(args):
    references = getattr(args, "reference_image", None) or []
    keyframes = getattr(args, "first_frame", None) or getattr(args, "last_frame", None)
    if references and keyframes:
        raise ValueError("Choose first/last frames OR general references, not both.")
    if len(references) > 9:
        raise ValueError("H3 supports at most nine reference images.")
    return "ref2va" if references else "fl2va" if keyframes else "t2va"


def image_inputs(args):
    """Decode local files only; never use the upstream URL-fetching convenience API."""
    from PIL import Image, ImageOps

    def read(path):
        with Image.open(Path(path)) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise ValueError("Reference inputs must be still images.")
            result = ImageOps.exif_transpose(image).convert("RGB")
            if not 0.25 <= result.width / result.height <= 4:
                raise ValueError("Image aspect ratio must be between 1:4 and 4:1.")
            return result

    inputs = {}
    for arg, name in (("first_frame", "image"), ("last_frame", "last_image")):
        path = getattr(args, arg, None)
        if path:
            inputs[name] = read(path)
    references = getattr(args, "reference_image", None) or []
    if references:
        from diffusers.modular_pipelines.minimax_h3 import MiniMaxH3ImageReference

        inputs["references"] = [
            MiniMaxH3ImageReference(read(path)) for path in references
        ]
    return inputs


def load_pipeline(workflow="t2va"):
    import torch
    from diffusers import MiniMaxH3Transformer3DModel, ModularPipeline, TorchAoConfig
    from diffusers.hooks import apply_group_offloading
    from torchao.quantization import Int8WeightOnlyConfig
    from transformers import Qwen3VLForConditionalGeneration
    from transformers import TorchAoConfig as TransformersTorchAoConfig

    if workflow not in ("t2va", "fl2va", "ref2va"):
        raise ValueError("Unknown H3 workflow.")
    transformer_name = "transformer_ref" if workflow == "ref2va" else "transformer"
    if not (MODEL_PATH / transformer_name / "config.json").is_file():
        raise RuntimeError(
            "Transformer weights are missing from the Docker model volume."
        )
    if not (MODEL_PATH / "modular_model_index.json").is_file():
        raise RuntimeError("Model weights are missing from the Docker model volume.")
    source = str(MODEL_PATH)
    pipe = ModularPipeline.from_pretrained(
        source, workflow=workflow, local_files_only=True
    )
    print(f"Loading and quantizing {transformer_name} to INT8…", flush=True)
    transformer = MiniMaxH3Transformer3DModel.from_pretrained(
        source,
        subfolder=transformer_name,
        dtype=torch.bfloat16,
        local_files_only=True,
        low_cpu_mem_usage=True,
        quantization_config=TorchAoConfig(
            Int8WeightOnlyConfig(version=2),
            modules_to_not_convert=[
                "proj_in",
                "audio_proj_in",
                "context_embedder",
                "time_embedder",
                "time_proj",
                "token_refiner",
                "norm_out",
                "proj_out",
                "audio_proj_out",
            ],
        ),
    )
    pipe.update_components(**{transformer_name: transformer})
    print("Loading and quantizing Qwen3-VL conditioner to INT8…", flush=True)
    pipe.update_components(
        text_encoder=Qwen3VLForConditionalGeneration.from_pretrained(
            source,
            subfolder="text_encoder",
            dtype=torch.bfloat16,
            local_files_only=True,
            quantization_config=TransformersTorchAoConfig(
                Int8WeightOnlyConfig(version=2),
                modules_to_not_convert=[
                    "model.visual",
                    "model.language_model.embed_tokens",
                    "model.language_model.norm",
                    "lm_head",
                ],
            ),
        ),
    )
    # Override embedded Hub paths so every remaining component is loaded locally.
    pipe.load_components(
        pretrained_model_name_or_path=source,
        local_files_only=True,
        dtype={"default": torch.bfloat16, "audio_vae": torch.float32},
    )
    required = [
        transformer_name if name == "transformer" else name for name in COMPONENTS
    ]
    missing = [name for name in required if getattr(pipe, name, None) is None]
    if missing:
        raise RuntimeError(f"Required components failed to load: {', '.join(missing)}")
    transformer.requires_grad_(False)
    pipe.text_encoder.requires_grad_(False)
    offload = {
        "onload_device": torch.device("cuda"),
        "offload_device": torch.device("cpu"),
        "use_stream": True,
    }
    transformer.enable_group_offload(
        offload_type="block_level",
        num_blocks_per_group=1,
        **offload,
    )
    apply_group_offloading(
        pipe.text_encoder.model, offload_type="leaf_level", **offload
    )
    pipe.vae.to("cuda")
    pipe.audio_vae.to("cuda")
    return pipe


@contextmanager
def gpu_owner():
    """Ensure only one provider worker owns the GPU."""
    with (ROOT / ".gpu.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another H3 worker owns the GPU.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def attach_progress(pipe, on_progress):
    """Replace only the stateless denoise block, preserving the upstream algorithm."""
    blocks = pipe.blocks
    original = blocks.sub_blocks["denoise.denoise"]

    class ReportingDenoiseStep(type(original)):
        def loop_step(self, components, state, *, i, t):
            total = len(state.timesteps)
            if i == 0:
                on_progress(0, total)
            result = super().loop_step(components, state, i=i, t=t)
            on_progress(i + 1, total)
            return result

    # `pipe.blocks` is a deep copy, not the executable graph. Build a lightweight
    # pipeline around the modified blocks and reuse the exact loaded components.
    blocks.sub_blocks["denoise.denoise"] = ReportingDenoiseStep()
    reporting_pipe = type(pipe)(blocks=blocks, modular_config_dict=dict(pipe.config))
    reporting_pipe.update_components(**pipe.components)
    return reporting_pipe


def generate(
    args: argparse.Namespace, pipe=None, hardware=None, on_progress=None, on_phase=None
) -> dict:
    validate_generation(args)
    # No hosted prompt enhancement, API calls, or downloads during inference.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import torch
    from diffusers.utils.export_utils import encode_video

    workflow = generation_workflow(args)
    inputs = image_inputs(args)
    started = time.monotonic()
    cold_start = pipe is None
    if cold_start:
        hardware = check_hardware()
        pipe = load_pipeline(workflow)
    if on_progress is not None:
        pipe = attach_progress(pipe, on_progress)
    frames = frame_count(args.seconds)
    print(f"Generating {frames} frames at {args.width}×{args.height}…", flush=True)
    torch.cuda.reset_peak_memory_stats()
    # TorchAO INT8 asynchronous transfers support no_grad, but not inference_mode.
    with torch.no_grad():
        result = pipe(
            prompt=args.prompt,
            width=args.width,
            height=args.height,
            num_frames=frames,
            num_inference_steps=args.steps,
            generator=torch.Generator().manual_seed(args.seed),
            output=["videos", "audio", "sampling_rate"],
            **inputs,
        )
    if on_phase is not None:
        on_phase("encoding")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encode_video(
        result["videos"][0],
        fps=24,
        output_path=str(args.output),
        audio=result["audio"][0],
        audio_sample_rate=result["sampling_rate"],
    )
    metadata = {
        "ai_generated": True,
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "workflow": workflow,
        "reference_image_count": len(inputs.get("references", [])),
        "frame_roles": [name for name in ("image", "last_image") if name in inputs],
        "prompt": args.prompt,
        "seed": args.seed,
        "frames": frames,
        "fps": 24,
        "width": args.width,
        "height": args.height,
        "sigma_grid_points": args.steps,
        "cold_start": cold_start,
        "elapsed_seconds_including_load": round(time.monotonic() - started, 2),
        "peak_allocated_vram_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        "hardware": hardware,
    }
    args.output.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Saved AI-generated video and metadata: {args.output.resolve()}", flush=True)
    return metadata


def main() -> None:
    """Internal model-provisioning command used by the Docker entrypoint."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    provisioning = commands.add_parser(
        "download",
        help="Download pinned text/keyframe weights; optionally general references",
    )
    provisioning.add_argument(
        "--references",
        action="store_true",
        help="Also download transformer_ref (61.73 GiB)",
    )
    args = parser.parse_args()
    try:
        download(include_references=args.references)
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
