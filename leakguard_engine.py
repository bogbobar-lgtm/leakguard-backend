import pandas as pd
import re, uuid
from datetime import datetime

def norm_text(x):
    if pd.isna(x): return ""
    return re.sub(r"\s+", " ", str(x).strip()).upper()

def norm_amount(x):
    try: return round(float(x), 2)
    except: return None

def normalise_ap(df):
    required=["supplier_id","supplier_name","invoice_number","invoice_date","due_date","payment_date","gross_amount","currency","PO_number","bank_reference","credit_note_flag"]
    missing=[c for c in required if c not in df.columns]
    if missing: raise ValueError("Missing AP columns: "+", ".join(missing))
    df=df.copy()
    for c in ["supplier_id","supplier_name","invoice_number","PO_number","bank_reference","currency"]: df[c+"_key"]=df[c].map(norm_text)
    for c in ["invoice_date","due_date","payment_date"]: df[c]=pd.to_datetime(df[c],errors="coerce")
    df["amount"]=df["gross_amount"].map(norm_amount)
    df["credit_note_flag"]=df["credit_note_flag"].astype(str).str.lower().isin(["true","1","yes","y"])
    df["row_id"]=range(1,len(df)+1)
    return df

def normalise_ar(df):
    required=["customer_id","customer_name","invoice_number","invoice_date","due_date","gross_amount","currency","payment_date","outstanding_amount","credit_note_flag"]
    missing=[c for c in required if c not in df.columns]
    if missing: raise ValueError("Missing AR columns: "+", ".join(missing))
    df=df.copy()
    for c in ["customer_id","customer_name","invoice_number","currency"]: df[c+"_key"]=df[c].map(norm_text)
    for c in ["invoice_date","due_date","payment_date"]: df[c]=pd.to_datetime(df[c],errors="coerce")
    df["amount"]=df["gross_amount"].map(norm_amount)
    df["outstanding"]=df["outstanding_amount"].map(norm_amount).fillna(0)
    df["credit_note_flag"]=df["credit_note_flag"].astype(str).str.lower().isin(["true","1","yes","y"])
    df["row_id"]=range(1,len(df)+1)
    return df

def duplicate_payments(ap):
    paid=ap[(ap["payment_date"].notna())&(ap["amount"].notna())&(~ap["credit_note_flag"])]
    findings=[]
    for _,g in paid.groupby(["supplier_id_key","invoice_number_key","amount","currency_key"],dropna=False):
        if len(g)>1 and g["invoice_number_key"].iloc[0]!="":
            value=round(g["amount"].iloc[0]*(len(g)-1),2); rows=g["row_id"].tolist()
            findings.append({"finding_id":"DUP-"+uuid.uuid4().hex[:8].upper(),"finding_type":"Duplicate Payment","confidence":"High","status":"New","supplier_or_customer":g["supplier_name"].iloc[0],"invoice_numbers":g["invoice_number"].tolist(),"dates":[d.strftime("%Y-%m-%d") for d in g["payment_date"]],"amount":g["amount"].iloc[0],"currency":g["currency"].iloc[0],"potential_recovery":value,"validated_recovery":0,"actual_recovered":0,"evidence":f"AP source rows {rows}; same supplier + invoice number + amount + currency with multiple payments.","rule_triggered":"AP_DUPLICATE_EXACT","explanation":"Multiple payments match the same supplier, invoice number, amount and currency.","recommended_action":"Validate against bank statement and supplier account, then request refund or offset.","source_rows":rows})
    return findings

def supplier_credits(ap):
    credits=ap[(ap["credit_note_flag"])&(ap["amount"]<0)]; findings=[]
    for _,r in credits.iterrows():
        value=abs(r["amount"]); findings.append({"finding_id":"CR-"+uuid.uuid4().hex[:8].upper(),"finding_type":"Unused Supplier Credit","confidence":"Medium","status":"New","supplier_or_customer":r["supplier_name"],"invoice_numbers":[r["invoice_number"]],"dates":[r["invoice_date"].strftime("%Y-%m-%d") if pd.notna(r["invoice_date"]) else ""],"amount":value,"currency":r["currency"],"potential_recovery":value,"validated_recovery":0,"actual_recovered":0,"evidence":f"AP source row {r['row_id']}; credit note flagged for {value:.2f}.","rule_triggered":"AP_UNUSED_CREDIT_NOTE","explanation":"A supplier credit note exists and requires matching to invoices/account balance before recovery can be validated.","recommended_action":"Confirm the credit remains unused and request application/refund.","source_rows":[int(r["row_id"])]})
    return findings

def overdue_ar(ar,analysis_date=None):
    asof=pd.Timestamp(analysis_date or datetime.now().date()); overdue=ar[(ar["outstanding"]>0)&(ar["due_date"].notna())&(ar["due_date"]<asof)]; findings=[]
    for _,r in overdue.iterrows():
        days=int((asof-r["due_date"]).days); bucket="1-30" if days<=30 else "31-60" if days<=60 else "61-90" if days<=90 else "91-180" if days<=180 else "181+"
        findings.append({"finding_id":"AR-"+uuid.uuid4().hex[:8].upper(),"finding_type":"Overdue AR","confidence":"High","status":"New","supplier_or_customer":r["customer_name"],"invoice_numbers":[r["invoice_number"]],"dates":[r["due_date"].strftime("%Y-%m-%d")],"amount":r["outstanding"],"currency":r["currency"],"potential_recovery":r["outstanding"],"validated_recovery":0,"actual_recovered":0,"evidence":f"AR source row {r['row_id']}; due {r['due_date'].date()}, {days} days overdue, outstanding {r['outstanding']:.2f}.","rule_triggered":f"AR_OVERDUE_{bucket}","explanation":f"Invoice is {days} days overdue with an outstanding balance.","recommended_action":"Prioritise collection follow-up and document payment commitment or dispute status.","source_rows":[int(r["row_id"])],"ageing_bucket":bucket})
    return findings

def run(ap_csv=None,ar_csv=None,analysis_date=None):
    findings=[]; records=0
    if ap_csv:
        ap=normalise_ap(pd.read_csv(ap_csv)); records+=len(ap); findings+=duplicate_payments(ap)+supplier_credits(ap)
    if ar_csv:
        ar=normalise_ar(pd.read_csv(ar_csv)); records+=len(ar); findings+=overdue_ar(ar,analysis_date)
    return {"analysis_run_id":"RUN-"+uuid.uuid4().hex[:10].upper(),"started_at":datetime.utcnow().isoformat()+"Z","completed_at":datetime.utcnow().isoformat()+"Z","records_received":records,"records_processed":records,"records_rejected":0,"candidate_count":len(findings),"high_confidence_count":sum(x["confidence"]=="High" for x in findings),"potential_recovery_total":round(sum(x["potential_recovery"] for x in findings),2),"validated_recovery_total":0,"actual_recovered_total":0,"findings":findings}
