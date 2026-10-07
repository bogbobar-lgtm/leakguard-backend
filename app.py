from fastapi import FastAPI, UploadFile, File, HTTPException, Header
from typing import Optional
import tempfile, os, importlib.util
from pathlib import Path

ENGINE=Path(__file__).parent/"leakguard_engine.py"
spec=importlib.util.spec_from_file_location("leakguard_engine",ENGINE); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
app=FastAPI(title="LeakGuard Finance Engine",version="0.1")
API_KEY=os.getenv("API_KEY")
MAX_MB=int(os.getenv("MAX_UPLOAD_MB","25"))

@app.get("/health")
def health(): return {"status":"ok","engine_version":"0.1","tests":["duplicate_payments","supplier_credits","overdue_ar"]}

@app.post("/analyze")
async def analyze(ap_file:Optional[UploadFile]=File(None),ar_file:Optional[UploadFile]=File(None),analysis_date:Optional[str]=None,x_api_key:Optional[str]=Header(None)):
    if API_KEY and x_api_key!=API_KEY: raise HTTPException(401,"Invalid API key")
    if not ap_file and not ar_file: raise HTTPException(400,"Provide ap_file and/or ar_file")
    paths=[]
    try:
        kwargs={}
        for label,upload in [("ap",ap_file),("ar",ar_file)]:
            if upload:
                if Path(upload.filename or "").suffix.lower() not in [".csv"]: raise HTTPException(400,f"{label.upper()} file must be CSV for v0.1")
                fd,path=tempfile.mkstemp(suffix=".csv"); os.close(fd); paths.append(path)
                data=await upload.read()
                if len(data)>MAX_MB*1024*1024: raise HTTPException(413,"File exceeds configured size limit")
                with open(path,"wb") as f: f.write(data)
                kwargs[label+"_csv"]=path
        return mod.run(analysis_date=analysis_date,**kwargs)
    finally:
        for p in paths:
            try: os.remove(p)
            except OSError: pass
