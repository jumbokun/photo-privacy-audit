"""Run a local Transformers image-encoder smoke test on a prepared checkpoint."""
from pathlib import Path
import sys

import torch
from PIL import Image
from transformers import AutoImageProcessor, AutoModel


def main(model_dir: str):
    model_dir = Path(model_dir).resolve()
    processor = AutoImageProcessor.from_pretrained(model_dir, local_files_only=True)
    model = AutoModel.from_pretrained(model_dir, local_files_only=True).eval()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    image = Image.new("RGB", (224, 224), (40, 80, 120))
    inputs = processor(images=image, return_tensors="pt")
    inputs = {key: value.to(device) for key, value in inputs.items()}
    with torch.inference_mode():
        if hasattr(model, "get_image_features"):
            embedding = model.get_image_features(pixel_values=inputs["pixel_values"])
        else:
            outputs = model(**inputs)
            embedding = getattr(outputs, "pooler_output", None)
            if embedding is None:
                embedding = getattr(outputs, "image_embeds", None)
            if embedding is None:
                embedding = outputs.last_hidden_state.mean(dim=1)
    embedding = torch.nn.functional.normalize(embedding.float(), dim=-1)
    print({
        "model_dir": str(model_dir),
        "device": str(device),
        "embedding_shape": list(embedding.shape),
        "embedding_norm": float(embedding.norm(dim=-1).mean()),
    })


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: dino_smoke.py MODEL_DIR")
    main(sys.argv[1])
