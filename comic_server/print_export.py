"""ICC-managed CMYK PDF writer. Converts strips; does not claim PDF/X certification."""
import hashlib
import io
import json
import zlib
from pathlib import Path

from PIL import Image, ImageCms


def cmyk_pdf(sources, profile_path, output, dpi=300, bleed_mm=0, rendering_intent=1):
    if dpi not in (300,600) or not 0<=bleed_mm<=6:
        raise ValueError("Choose print resolution 300/600 and bleed 0–6 mm")
    profile_bytes=Path(profile_path).read_bytes()
    if not 128<=len(profile_bytes)<=4*1024*1024 or profile_bytes[16:20]!=b"CMYK" or profile_bytes[36:40]!=b"acsp":
        raise ValueError("Select a valid CMYK printer ICC profile (up to 4 MB)")
    target=ImageCms.ImageCmsProfile(io.BytesIO(profile_bytes))
    source=ImageCms.createProfile("sRGB")
    transform=ImageCms.buildTransformFromOpenProfiles(source,target,"RGB","CMYK",renderingIntent=rendering_intent)
    width_pt=(210+2*bleed_mm)*72/25.4;height_pt=(297+2*bleed_mm)*72/25.4
    bleed_pt=bleed_mm*72/25.4
    expected=(round((210+2*bleed_mm)*dpi/25.4),round((297+2*bleed_mm)*dpi/25.4))
    maximum_ink=0;offsets={};objects=6+3*len(sources)
    def obj(out,index,body):
        offsets[index]=out.tell();out.write(f"{index} 0 obj\n".encode());out.write(body);out.write(b"\nendobj\n")
    def stream(out,index,metadata,file=None,data=None):
        length=Path(file).stat().st_size if file else len(data)
        offsets[index]=out.tell();out.write(f"{index} 0 obj\n<< {metadata} /Length {length} >>\nstream\n".encode())
        if file:
            with open(file,"rb") as input:
                while True:
                    chunk=input.read(65536)
                    if not chunk:break
                    out.write(chunk)
        else:out.write(data)
        out.write(b"\nendstream\nendobj\n")
    staging=Path(str(output)+".part")
    try:
        with staging.open("wb") as out:
            out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
            obj(out,1,b"<< /Type /Catalog /Pages 2 0 R /OutputIntents [5 0 R] >>")
            obj(out,2,("<< /Type /Pages /Count "+str(len(sources))+" /Kids ["+" ".join(f"{6+i*3} 0 R" for i in range(len(sources)))+"] >>").encode())
            stream(out,3,"/N 4 /Alternate /DeviceCMYK",data=profile_bytes)
            obj(out,4,b"<< /Producer (ComicAI Forge ICC print engine) >>")
            obj(out,5,b"<< /Type /OutputIntent /S /GTS_PDFX /OutputConditionIdentifier (User selected printer ICC profile) /DestOutputProfile 3 0 R >>")
            for index,path in enumerate(sources):
                compressed=Path(str(output)+f".{index}.z")
                try:
                    with Image.open(path) as image:
                        if abs(image.width-expected[0])>2 or abs(image.height-expected[1])>2:
                            raise ValueError("Print page dimensions do not match the chosen A4 resolution/bleed")
                        if image.width*image.height>40_000_000:
                            raise ValueError("Print page exceeds the 40 megapixel limit")
                        image.load();compressor=zlib.compressobj(6)
                        with compressed.open("wb") as encoded:
                            for top in range(0,image.height,128):
                                rgb=image.crop((0,top,image.width,min(image.height,top+128))).convert("RGB")
                                converted=ImageCms.applyTransform(rgb,transform)
                                raw=converted.tobytes()
                                maximum_ink=max(maximum_ink,max(sum(raw[i:i+4]) for i in range(0,len(raw),4))*100/255)
                                encoded.write(compressor.compress(raw))
                            encoded.write(compressor.flush())
                        w,h=image.size
                    page=6+index*3;image_id=page+1;content=page+2
                    stream(out,image_id,f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /BitsPerComponent 8 /ColorSpace [/ICCBased 3 0 R] /Filter /FlateDecode",file=compressed)
                    stream(out,content,"",data=f"q {width_pt:.5f} 0 0 {height_pt:.5f} 0 0 cm /Im0 Do Q".encode())
                    obj(out,page,(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:.5f} {height_pt:.5f}] /BleedBox [0 0 {width_pt:.5f} {height_pt:.5f}] /TrimBox [{bleed_pt:.5f} {bleed_pt:.5f} {width_pt-bleed_pt:.5f} {height_pt-bleed_pt:.5f}] /Resources << /XObject << /Im0 {image_id} 0 R >> >> /Contents {content} 0 R >>").encode())
                finally:compressed.unlink(missing_ok=True)
            xref=out.tell();out.write(f"xref\n0 {objects}\n0000000000 65535 f \n".encode())
            for number in range(1,objects):out.write(f"{offsets[number]:010d} 00000 n \n".encode())
            out.write(f"trailer\n<< /Size {objects} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        staging.replace(output)
        return {"mode":"CMYK","dpi":dpi,"pages":len(sources),"iccSha256":hashlib.sha256(profile_bytes).hexdigest(),
            "profileName":ImageCms.getProfileName(target).strip(),"maximumInkPercent":round(maximum_ink,1),
            "notice":"ICC-managed raster PDF; no PDF/X certification. Confirm paper, ink limits and proof with your printer."}
    finally:staging.unlink(missing_ok=True)
