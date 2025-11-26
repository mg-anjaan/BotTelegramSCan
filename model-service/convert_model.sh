#!/usr/bin/env bash
# Convert a PyTorch checkpoint to ONNX (example)
set -e
CHECKPOINT=$1
OUT=$2
if [ -z "$CHECKPOINT" ] || [ -z "$OUT" ]; then
  echo "Usage: $0 checkpoint.pth out.onnx"
  exit 1
fi
python - <<PY
import torch
from torchvision import models
model = models.resnet18(pretrained=False)
num_ftrs = model.fc.in_features
model.fc = torch.nn.Linear(num_ftrs, 1)
ckpt = torch.load('$CHECKPOINT', map_location='cpu')
model.load_state_dict(ckpt)
model.eval()
x = torch.randn(1,3,224,224)
torch.onnx.export(model, x, '$OUT', opset_version=11, input_names=['input'], output_names=['output'], dynamic_axes={'input':{0:'batch'}, 'output':{0:'batch'}})
print('Exported to $OUT')
PY
