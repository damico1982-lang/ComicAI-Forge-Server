"""Fetch licensed OpenCV models during image build, pinning bytes by SHA-256."""
import hashlib
from pathlib import Path
from urllib.request import urlopen

root=Path(__file__).parent/"vision_models"
root.mkdir(exist_ok=True)
models={
    "yunet.onnx":("face_detection_yunet/face_detection_yunet_2023mar.onnx","8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "sface.onnx":("face_recognition_sface/face_recognition_sface_2021dec.onnx","0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}
for name,(path,digest) in models.items():
    destination=root/name
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest()==digest:
        continue
    with urlopen("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"+path,timeout=120) as response:
        content=response.read(40_000_001)
    if len(content)>40_000_000 or hashlib.sha256(content).hexdigest()!=digest:
        raise RuntimeError("OpenCV model checksum mismatch; build aborted")
    destination.write_bytes(content)
