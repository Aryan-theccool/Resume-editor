from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.responses import FileResponse
import os
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import re
import ollama

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

resumes = {}

class EditRequest(BaseModel):
    resume_id: str
    instruction: str

class PasteRequest(BaseModel):
    latex_code: str

# Serve frontend
@app.get("/")
async def serve_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        return FileResponse(frontend_path)
    return {"message": "Frontend not found", "path": frontend_path}

@app.get("/api/health")
async def health_check():
    try:
        # Use the ollama library to check connection
        ollama.list()
        return {"status": "ok", "ollama": "connected"}
    except Exception as e:
        print(f"Ollama connection error: {e}")
        return {"status": "error", "ollama": "not connected"}

@app.post("/paste")
async def paste_resume(request: PasteRequest):
    latex_code = request.latex_code.strip()
    
    if not latex_code:
        raise HTTPException(400, "No code")
    
    resume_id = f"resume_{len(resumes)}"
    resumes[resume_id] = {
        "filename": "resume.tex",
        "original": latex_code,
        "current": latex_code
    }
    
    return {"resume_id": resume_id, "message": "Loaded"}

@app.post("/upload")
async def upload_resume(file: UploadFile):
    content = await file.read()
    latex_code = content.decode("utf-8")
    
    resume_id = f"resume_{len(resumes)}"
    resumes[resume_id] = {
        "filename": file.filename,
        "original": latex_code,
        "current": latex_code
    }
    
    return {"resume_id": resume_id, "message": "Uploaded"}

@app.post("/edit")
async def edit_resume(request: EditRequest):
    if request.resume_id not in resumes:
        raise HTTPException(404, "Resume not found")
    
    latex_code = resumes[request.resume_id]["current"]
    instruction = request.instruction.lower()
    
    print(f"\n{'='*60}")
    print(f"EDITING: {request.instruction}")
    print(f"{'='*60}")
    
    # Simple regex-based edits (NO AI - just regex)
    modified = latex_code
    
    # MAKE BOLD: "make X bold" or "bold X"
    if "bold" in instruction:
        # Extract what to make bold
        words = request.instruction.split()
        for i, word in enumerate(words):
            if word.lower() in ["make", "bold"]:
                if i + 1 < len(words):
                    target = words[i + 1]
                    # Make it bold
                    pattern = rf'\b{re.escape(target)}\b'
                    replacement = rf'\\textbf{{{target}}}'
                    modified = re.sub(pattern, replacement, modified)
                    print(f"Made '{target}' bold")
    
    # REMOVE: "remove X" or "delete X"
    elif "remove" in instruction or "delete" in instruction:
        words = request.instruction.split()
        for i, word in enumerate(words):
            if word.lower() in ["remove", "delete"]:
                if i + 1 < len(words):
                    target = words[i + 1]
                    # Remove the line containing it
                    lines = modified.split('\n')
                    modified = '\n'.join([line for line in lines if target.lower() not in line.lower()])
                    print(f"Removed '{target}'")
    
    # HIGHLIGHT: "highlight X"
    elif "highlight" in instruction:
        words = request.instruction.split()
        for i, word in enumerate(words):
            if word.lower() == "highlight":
                if i + 1 < len(words):
                    target = words[i + 1]
                    pattern = rf'\b{re.escape(target)}\b'
                    replacement = rf'\\textbf{{{target}}}'
                    modified = re.sub(pattern, replacement, modified)
                    print(f"Highlighted '{target}'")
    
    # If no simple rule matched, use AI
    else:
        print("Using AI to edit...")
        modified = await use_ai_edit(latex_code, request.instruction)
    
    # Save
    resumes[request.resume_id]["current"] = modified
    
    print(f"Modified length: {len(modified)} chars")
    print(f"{'='*60}\n")
    
    return {"latex_code": modified, "message": "Done"}

async def use_ai_edit(latex_code: str, instruction: str) -> str:
    """Full Document AI Editing as requested"""
    
    print(f"\n============================================================")
    print(f"EDITING FULL DOCUMENT: {instruction}")
    print(f"============================================================")
    
    prompt = rf"""You are a professional LaTeX resume editor.
Your task: Modify the provided LaTeX code according to the user's instruction.

RULES:
1. Return the FULL document starting from \documentclass.
2. Maintain the EXACT existing formatting and all custom LaTeX macros.
3. Integrate the changes naturally.
4. Do NOT use "..." or summaries. Output every single line.

USER INSTRUCTION: {instruction}

ORIGINAL LATEX CODE:
{latex_code}
"""
    
    try:
        print("AI is processing the full document (this will take 2-4 mins)...", end="", flush=True)
        response = ollama.generate(
            model='phi3:mini',
            prompt=prompt,
            stream=False,
            options={
                'temperature': 0.1,
                'num_ctx': 8192,
                'num_predict': 8192,
            }
        )
        
        modified_code = response.get('response', '').strip()
        print(" Done.")
        
        # 1. Clean up markdown code blocks
        if "```" in modified_code:
            match = re.search(r'```(?:latex)?\n?(.*?)\n?```', modified_code, re.DOTALL)
            if match:
                modified_code = match.group(1).strip()
            else:
                modified_code = '\n'.join([l for l in modified_code.split('\n') if "```" not in l]).strip()

        # 2. Fix common AI-generated syntax errors
        # Ensure only ONE document preamble exists
        if modified_code.count("\\documentclass") > 1:
            parts = modified_code.split("\\documentclass")
            modified_code = "\\documentclass" + parts[-1]
        
        # Fix unescaped ampersands in common text areas
        modified_code = re.sub(r'(?<!\\)&', r'\\&', modified_code)

        # 3. Fix common hallucinations
        hallucinations = {
            r'\\endin': r'\\end{document}',
            r'\\end{doc}': r'\\end{document}',
            r'\\enddoc': r'\\end{document}'
        }
        for h, replacement in hallucinations.items():
            modified_code = re.sub(h, replacement, modified_code)

        if not modified_code or len(modified_code) < 100:
            print("AI failed to generate output. Returning original.")
            return latex_code
            
        return modified_code
            
    except Exception as e:
        print(f"AI error: {e}")
        return latex_code

@app.get("/resume/{resume_id}")
async def get_resume(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    return {"latex_code": resumes[resume_id]["current"]}

@app.post("/reset/{resume_id}")
async def reset_resume(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    
    resumes[resume_id]["current"] = resumes[resume_id]["original"]
    return {"latex_code": resumes[resume_id]["original"]}

if __name__ == "__main__":
    import uvicorn
    print("\nRESUME EDITOR - READY\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)