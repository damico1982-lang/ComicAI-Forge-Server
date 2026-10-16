"""Private likeness review and free-space suggestions; never an identity guarantee."""
import hashlib
import io
import json
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

MODEL_VERSION = "opencv-yunet-2023mar-sface-2021dec"


class CharacterVision:
    def __init__(self, store, directory=None):
        self.store = store
        directory = Path(directory or os.getenv("COMIC_VISION_MODELS", Path(__file__).parent / "vision_models"))
        detector = directory / "yunet.onnx"
        recognizer = directory / "sface.onnx"
        self.available = detector.is_file() and recognizer.is_file()
        self.detector = self.recognizer = None
        if self.available:
            cv2.setNumThreads(1)
            self.detector = cv2.FaceDetectorYN.create(str(detector), "", (640, 640), .8, .3, 100)
            self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")
            self.version = MODEL_VERSION + ":" + hashlib.sha256(recognizer.read_bytes()).hexdigest()[:16]
        else:
            self.version = MODEL_VERSION + ":unavailable"
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS generated_face_embeddings (username TEXT NOT NULL, job_id TEXT NOT NULL, panel_index INTEGER NOT NULL, face_index INTEGER NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, PRIMARY KEY(username,job_id,panel_index,face_index,model))")
            db.execute("CREATE TABLE IF NOT EXISTS approved_character_faces (username TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, source_job TEXT NOT NULL, face_index INTEGER NOT NULL, PRIMARY KEY(username,name,model))")
            db.execute("CREATE TABLE IF NOT EXISTS character_embeddings (username TEXT NOT NULL, upload_id TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL, vector BLOB NOT NULL, landmarks TEXT NOT NULL, PRIMARY KEY(username,upload_id,model))")

    def detect(self, content, embeddings=False):
        if not self.available:
            return []
        image = cv2.imdecode(np.frombuffer(content, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            return []
        height,width=image.shape[:2]
        scale=min(1.,640/max(width,height))
        image=cv2.resize(image,(max(1,int(width*scale)),max(1,int(height*scale))))
        height,width=image.shape[:2]
        self.detector.setInputSize((width,height))
        _,faces=self.detector.detect(image)
        output=[]
        for face in ([] if faces is None else faces[:12]):
            x,y,w,h=map(float,face[:4])
            rect=[max(0.,(x-w*.12)/width),max(0.,(y-h*.2)/height),min(1.,(x+w*1.12)/width),min(1.,(y+h*1.15)/height)]
            item={"rect":rect,"landmarks":[[float(face[i])/width,float(face[i+1])/height] for i in range(4,14,2)],"confidence":float(face[14])}
            if embeddings:
                vector=self.recognizer.feature(self.recognizer.alignCrop(image,face)).reshape(-1).copy()
                vector=vector/max(float(np.linalg.norm(vector)),1e-8)
                item["vector"]=vector
            output.append(item)
        return output

    def reference_vectors(self, username, selected):
        output=[]
        for ref in selected:
            if not (ref.useForAi and ref.role=="CHARACTER"):
                continue
            row=self.store.upload(str(ref.id),username)
            if row is None:
                continue
            with self.store.connect() as db:
                saved=db.execute("SELECT * FROM character_embeddings WHERE username=? AND upload_id=? AND model=?",(username,str(ref.id),self.version)).fetchone()
            if saved:
                output.append((ref.label,np.frombuffer(saved["vector"],dtype=np.float32).copy()))
                continue
            faces=self.detect((self.store.uploads/row["disk_name"]).read_bytes(),True)
            # A crowded reference cannot silently assign somebody else's embedding.
            if len(faces)!=1:
                continue
            face=faces[0]
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO character_embeddings VALUES (?,?,?,?,?,?)",(username,str(ref.id),ref.label,self.version,face["vector"].astype(np.float32).tobytes(),json.dumps(face["landmarks"])))
            output.append((ref.label,face["vector"]))
        names={ref.label for ref in selected if ref.useForAi and ref.role=="CHARACTER"}
        with self.store.connect() as db:
            for row in db.execute("SELECT name,vector FROM approved_character_faces WHERE username=? AND model=?",(username,self.version)):
                if row["name"] in names:output.append((row["name"],np.frombuffer(row["vector"],dtype=np.float32).copy()))
        return output

    def analyze(self, content, panel, username, selected, compare=False, job_id=None, panel_index=None):
        faces=self.detect(content,compare)
        if compare and job_id is not None and panel_index is not None:
            with self.store.connect() as db:
                for index,face in enumerate(faces):
                    db.execute("INSERT OR REPLACE INTO generated_face_embeddings VALUES (?,?,?,?,?,?)",(username,job_id,panel_index,index,self.version,face["vector"].astype(np.float32).tobytes()))
        checks=[]
        if compare:
            refs=self.reference_vectors(username,selected)
            for name in dict.fromkeys(name for name,_ in refs):
                candidates=[float(np.dot(face["vector"],v)) for face in faces for label,v in refs if label==name]
                score=max(candidates) if candidates else None
                checks.append({"character":name,"cosineSimilarity":round(score,4) if score is not None else None,
                    "status":"possible_drift" if score is not None and score<.363 else "review_required",
                    "notice":"A photo-trained similarity check is not calibrated as proof of likeness in stylized comic art."})
            if not refs:
                checks.append({"character":"","cosineSimilarity":None,"status":"unverified","notice":"No single detectable face in the selected character references; review manually."})
        protected=[face["rect"] for face in faces]
        positions=suggest_positions(content,[panel.caption,panel.dialogue],protected)
        return {"model":self.version,"protectedRegions":protected,"landmarks":[f["landmarks"] for f in faces],
            "characterChecks":checks,"balloons":positions,
            "reviewRequired":not faces or any(c["status"]!="approved_by_user" for c in checks),
            "characterNames":list(dict.fromkeys(ref.label for ref in selected if ref.useForAi and ref.role=="CHARACTER")),
            "notice":"Face/free-space suggestions need visual review. No perfect-character or no-overlap guarantee."}


def intersection(a,b):
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))


def suggest_positions(content,texts,protected):
    with Image.open(io.BytesIO(content)) as original:
        gray=np.asarray(original.convert("L").resize((160,160)),dtype=np.uint8)
    edges=cv2.Canny(gray,60,150).astype(np.float32)/255
    integral=cv2.integral(edges)
    results=[];placed=[]
    for text in texts:
        if not text.strip():
            results.append(None);continue
        w=.72;h=min(.42,max(.14,len(text)/35*.045+.06))
        candidates=[]
        for y in np.linspace(.03,max(.03,.97-h),16):
            for x in np.linspace(.03,.97-w,7):
                rect=[float(x),float(y),float(x+w),float(y+h)]
                left,top,right,bottom=[int(v*159) for v in rect]
                edge=float(integral[bottom,right]-integral[top,right]-integral[bottom,left]+integral[top,left])/max(1,(right-left)*(bottom-top))
                protected_overlap=sum(intersection(rect,box) for box in protected)
                balloon_overlap=sum(intersection(rect,box) for box in placed)
                score=edge+1000*protected_overlap+2000*balloon_overlap+.005*y
                candidates.append((score,rect,protected_overlap+balloon_overlap))
        _,rect,overlap=min(candidates,key=lambda c:c[0]);placed.append(rect)
        results.append({"x":rect[0],"y":rect[1],"width":w,"height":h,"reviewRequired":overlap>0 or not protected})
    return results
