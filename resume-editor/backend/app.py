from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import os
import tempfile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import re
import ollama
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory storage: resume_id -> { filename, history: [ {latex, instruction, timestamp} ] }
resumes = {}

class EditRequest(BaseModel):
    resume_id: str
    instruction: str

class PasteRequest(BaseModel):
    latex_code: str

# ─── Serve Frontend ────────────────────────────────────────────────────────────
@app.get("/")
async def serve_frontend():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        return FileResponse(frontend_path)
    return {"message": "Frontend not found", "path": frontend_path}

# ─── Health Check ──────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health_check():
    try:
        ollama.list()
        return {"status": "ok", "ollama": "connected"}
    except Exception as e:
        print(f"Ollama connection error: {e}")
        return {"status": "error", "ollama": "not connected"}

# ─── Paste Resume ──────────────────────────────────────────────────────────────
@app.post("/paste")
async def paste_resume(request: PasteRequest):
    latex_code = request.latex_code.strip()
    if not latex_code:
        raise HTTPException(400, "No code provided")

    resume_id = f"resume_{len(resumes)}"
    resumes[resume_id] = {
        "filename": "resume.tex",
        "history": [
            {"latex": latex_code, "instruction": "Original", "timestamp": datetime.now().isoformat()}
        ]
    }
    return {"resume_id": resume_id, "message": "Loaded"}

# ─── Upload Resume ─────────────────────────────────────────────────────────────
@app.post("/upload")
async def upload_resume(file: UploadFile):
    content = await file.read()
    latex_code = content.decode("utf-8")

    resume_id = f"resume_{len(resumes)}"
    resumes[resume_id] = {
        "filename": file.filename,
        "history": [
            {"latex": latex_code, "instruction": "Original upload", "timestamp": datetime.now().isoformat()}
        ]
    }
    return {"resume_id": resume_id, "message": "Uploaded"}

# ─── Get Resume ────────────────────────────────────────────────────────────────
@app.get("/resume/{resume_id}")
async def get_resume(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    history = resumes[resume_id]["history"]
    return {
        "latex_code": history[-1]["latex"],
        "filename": resumes[resume_id]["filename"],
        "version": len(history) - 1,
        "total_versions": len(history)
    }

# ─── Edit Resume ───────────────────────────────────────────────────────────────
@app.post("/edit")
async def edit_resume(request: EditRequest):
    if request.resume_id not in resumes:
        raise HTTPException(404, "Resume not found")

    history = resumes[request.resume_id]["history"]
    latex_code = history[-1]["latex"]
    instruction = request.instruction.lower()

    print(f"\n{'='*60}")
    print(f"EDITING: {request.instruction}")
    print(f"{'='*60}")

    modified = latex_code
    used_ai = False

    # ── Smart fast-path router (no AI for simple targeted tasks) ────

    COMPLEX_KEYWORDS = {
        'professional', 'concise', 'rewrite', 'tailor', 'improve',
        'grammar', 'polish', 'compelling', 'stronger', 'quantify',
        'action verbs', 'summary', 'format', 'proofread', 'rephrase',
        'restructure', 'overhaul', 'ats', 'optimize'
    }
    
    # Identify if user is combining multiple operations (e.g. "remove X and add Y")
    action_verbs = ['add', 'remove', 'delete', 'change', 'replace', 'bold', 'highlight']
    has_multiple_actions = sum(1 for verb in action_verbs if verb in instruction) > 1
    
    is_complex = any(kw in instruction for kw in COMPLEX_KEYWORDS) or has_multiple_actions

    # ── Pattern matching (broad, catches natural language variants) ──

    # "add X to/in/under/as/as a/as an  skills/languages/tools/tech"
    # e.g. "add python as skill", "add python in skills", "add python to languages"
    add_to_section_m = re.search(
        r'add\s+([\w\s\+\#\.]+?)\s+(?:to|in|under|into|as(?:\s+a(?:n)?)?|as\s+(?:a\s+)?(?:new\s+)?)\s*'
        r'(skills?|languages?|tools?|technologies?|tech(?:nologies)?|programming)',
        instruction
    )

    # "add [text] to achievements/experience/projects/education/certifications"
    # e.g. "add won first place at XYZ to achievements"
    BULLET_SECTIONS = (
        'achievement', 'experience', 'project', 'education',
        'certification', 'award', 'extracurricular', 'activity',
        'publication', 'volunteer', 'leadership', 'honor'
    )
    add_to_bullet_m = re.search(
        r'add\s+(.+?)\s+(?:to|in|under|into)\s+(?:(?:my|the)\s+)?({}s?)'.format('|'.join(BULLET_SECTIONS)),
        instruction
    )

    # "add bullet/achievement/item: [text]" or "add bullet [text]"
    add_bullet_direct_m = re.match(
        r'add\s+(?:a\s+)?(?:bullet|item|achievement|point|entry)\s+(.+)',
        instruction
    )

    # "change X to Y" or "replace X with Y" or "rename X to Y"
    change_m = re.match(r'(?:change|replace|rename|update)\s+(.+?)\s+(?:to|with)\s+(.+)', instruction)

    # "add a/an <Name> section"
    add_section_m = re.match(r'add\s+(?:a\s+|an\s+)?(.+?)\s+section', instruction)

    # Generic "add X" — catches bare "add python", "add docker", "add python as a skill", etc.
    # Excludes instructions that start with "add a/an ... section"
    add_generic_m = re.match(
        r'^add\s+([\w][\w\s\+\#\.]*?)'
        r'(?:\s+(?:as\s+(?:a\s+|an\s+)?(?:skill|language|tool|technology))?'
        r'|\s+to\s+(?:my\s+)?(?:resume|cv))?$',
        instruction
    ) if not re.match(r'^add\s+(?:a|an)\s+', instruction) else None

    def insert_into_skills(code, item):
        """
        Surgically append `item` to the Programming Languages line (or any skills line).
        Specifically handles the common resume format:
            \textbf{Programming Languages:} C++, JavaScript, ...
        Returns (modified_code, success_bool).
        """
        lines = code.split('\n')

        # Priority 1: Look for an explicit 'Programming Languages' line
        for idx, line in enumerate(lines):
            if re.search(r'Programming Languages', line, re.IGNORECASE):
                if item.lower() not in line.lower():
                    # Line ends with \\ or \\[1pt] — insert before that
                    new_line = re.sub(r'(\\\\(?:\[\d+(?:\.\d+)?pt\])?)\s*$',
                                      f', {item}\\1', line.rstrip())
                    if new_line == line.rstrip():  # no trailing \\ found
                        new_line = line.rstrip() + f', {item}'
                    lines[idx] = new_line
                    print(f"[FAST] Inserted '{item}' into Programming Languages line")
                    return '\n'.join(lines), True
                else:
                    print(f"[FAST] '{item}' already in Programming Languages")
                    return code, True  # already there, not an error

        # Priority 2: Any skills/languages/tools section header → look ahead for content
        section_re = re.compile(r'(skill|language|tool|technolog|framework)', re.IGNORECASE)
        for idx, line in enumerate(lines):
            if section_re.search(line):
                for j in range(idx, min(idx + 10, len(lines))):
                    ln = lines[j]
                    if re.search(r'\w+,\s*\w+', ln) and not ln.strip().startswith('%'):
                        if item.lower() not in ln.lower():
                            stripped = ln.rstrip()
                            new_line = re.sub(r'(\\\\(?:\[\d+(?:\.\d+)?pt\])?)\s*$',
                                              f', {item}\\1', stripped)
                            if new_line == stripped:
                                new_line = stripped + f', {item}'
                            lines[j] = new_line
                            return '\n'.join(lines), True

        # Priority 3: First comma-list line in the document
        for idx, line in enumerate(lines):
            if (re.search(r'\w+,\s*\w+', line)
                    and not line.strip().startswith('%')
                    and '\\section' not in line):
                if item.lower() not in line.lower():
                    stripped = line.rstrip()
                    new_line = re.sub(r'(\\\\(?:\[\d+(?:\.\d+)?pt\])?)\s*$',
                                      f', {item}\\1', stripped)
                    if new_line == stripped:
                        new_line = stripped + f', {item}'
                    lines[idx] = new_line
                    return '\n'.join(lines), True

        return code, False

    def insert_bullet_into_section(code, section_keyword, bullet_text):
        """
        Find a \section{} whose name contains `section_keyword` and insert
        a new \item before the last \end{itemize} in that section.
        Returns (modified_code, success_bool).
        """
        lines = code.split('\n')
        section_re = re.compile(r'\\section\s*\{([^}]*)\}', re.IGNORECASE)

        # Find the section
        section_start = None
        for idx, line in enumerate(lines):
            m = section_re.search(line)
            if m and section_keyword.lower() in m.group(1).lower():
                section_start = idx
                break

        if section_start is None:
            # Try matching any line that has the keyword near \section
            for idx, line in enumerate(lines):
                if section_keyword.lower() in line.lower() and '\\section' in line:
                    section_start = idx
                    break

        if section_start is None:
            return code, False

        # Find the next \section or \end{document} to bound our search
        section_end = len(lines)
        for idx in range(section_start + 1, len(lines)):
            if section_re.search(lines[idx]) or '\\end{document}' in lines[idx]:
                section_end = idx
                break

        # Within that range, find the LAST \end{itemize}
        last_itemize_end = None
        for idx in range(section_end - 1, section_start, -1):
            if '\\end{itemize}' in lines[idx]:
                last_itemize_end = idx
                break

        if last_itemize_end is None:
            # No itemize found — try to append before the section boundary
            insert_at = section_end - 1
            indent = '  '
            new_item = f'{indent}\\item {bullet_text}'
            lines.insert(insert_at, new_item)
            return '\n'.join(lines), True

        # Determine indentation from existing \item lines in this section
        indent = '  '
        for idx in range(section_start, last_itemize_end):
            if '\\item' in lines[idx]:
                indent = re.match(r'^(\s*)', lines[idx]).group(1)
                break

        new_item = f'{indent}\\item {bullet_text}'
        lines.insert(last_itemize_end, new_item)
        print(f"[FAST] Inserted bullet into section at line {last_itemize_end}")
        return '\n'.join(lines), True

    # ── Route to correct handler ──────────────────────────────────────

    if "bold" in instruction and not is_complex:
        words = request.instruction.split()
        for i, word in enumerate(words):
            if word.lower() in ["make", "bold"]:
                if i + 1 < len(words):
                    target = words[i + 1]
                    pattern = rf'\b{re.escape(target)}\b'
                    replacement = rf'\\textbf{{{target}}}'
                    modified = re.sub(pattern, replacement, modified)
                    print(f"[FAST] Made '{target}' bold")

    elif ("remove" in instruction or "delete" in instruction) and not is_complex:
        words = request.instruction.split()
        for i, word in enumerate(words):
            if word.lower() in ["remove", "delete"]:
                if i + 1 < len(words):
                    target = ' '.join(words[i + 1:]).rstrip('.')
                    doc_lines = modified.split('\n')
                    modified = '\n'.join([l for l in doc_lines if target.lower() not in l.lower()])
                    print(f"[FAST] Removed lines containing '{target}'")

    elif "highlight" in instruction and not is_complex:
        words = request.instruction.split()
        for i, word in enumerate(words):
            if word.lower() == "highlight":
                if i + 1 < len(words):
                    target = words[i + 1]
                    pattern = rf'\b{re.escape(target)}\b'
                    replacement = rf'\\textbf{{{target}}}'
                    modified = re.sub(pattern, replacement, modified)
                    print(f"[FAST] Highlighted '{target}'")

    elif add_to_section_m and not is_complex:
        raw_item = add_to_section_m.group(1).strip()
        item = raw_item.title()
        print(f"[FAST] Adding '{item}' to skills section")
        modified, ok = insert_into_skills(modified, item)
        if not ok:
            print(f"[FAST] Could not locate skills line — NOT calling AI to avoid corruption")
            return {
                "latex_code": modified,
                "message": f"Could not find a skills/languages line to insert '{item}' into. "
                           f"Please manually add it to your Technical Skills section.",
                "version": len(resumes[request.resume_id]['history']) - 1,
                "total_versions": len(resumes[request.resume_id]['history'])
            }

    elif add_to_bullet_m and not is_complex:
        # "add [text] to achievements", "add [text] to experience", etc.
        bullet_text = add_to_bullet_m.group(1).strip()
        # Capitalise first letter
        bullet_text = bullet_text[0].upper() + bullet_text[1:] if bullet_text else bullet_text
        # Extract section name from the instruction (last word group after to/in/under)
        sec_match = re.search(
            r'(?:to|in|under|into)\s+(?:(?:my|the)\s+)?({}s?)'.format('|'.join(BULLET_SECTIONS)),
            instruction
        )
        section_kw = sec_match.group(1).rstrip('s') if sec_match else 'achievement'
        print(f"[FAST] Adding bullet to '{section_kw}' section: {bullet_text}")
        modified, ok = insert_bullet_into_section(modified, section_kw, bullet_text)
        if not ok:
            return {
                "latex_code": modified,
                "message": f"Could not find a '{section_kw}' section in the document. "
                           f"Make sure the section exists and try again.",
                "version": len(resumes[request.resume_id]['history']) - 1,
                "total_versions": len(resumes[request.resume_id]['history'])
            }

    elif add_bullet_direct_m and not is_complex:
        # "add bullet Won first place at XYZ"
        bullet_text = add_bullet_direct_m.group(1).strip()
        bullet_text = bullet_text[0].upper() + bullet_text[1:] if bullet_text else bullet_text
        # Try to insert into Achievements first, then last itemize in doc
        print(f"[FAST] Adding direct bullet: {bullet_text}")
        modified, ok = insert_bullet_into_section(modified, 'achievement', bullet_text)
        if not ok:
            # Fallback: insert before the very last \end{itemize} in the whole document
            lines = modified.split('\n')
            for idx in range(len(lines) - 1, -1, -1):
                if '\\end{itemize}' in lines[idx]:
                    lines.insert(idx, f'  \\item {bullet_text}')
                    modified = '\n'.join(lines)
                    ok = True
                    break
        if not ok:
            return {
                "latex_code": modified,
                "message": "Could not find an itemize list to add the bullet to.",
                "version": len(resumes[request.resume_id]['history']) - 1,
                "total_versions": len(resumes[request.resume_id]['history'])
            }

    elif change_m and not is_complex:
        old_val = change_m.group(1).strip()
        new_val = change_m.group(2).strip()
        if old_val.lower() in modified.lower():
            modified = re.sub(re.escape(old_val), new_val, modified, flags=re.IGNORECASE)
            print(f"[FAST] Replaced '{old_val}' → '{new_val}'")
        else:
            return {
                "latex_code": modified,
                "message": f"Could not find '{old_val}' in the document.",
                "version": len(resumes[request.resume_id]['history']) - 1,
                "total_versions": len(resumes[request.resume_id]['history'])
            }

    elif add_section_m and not is_complex:
        section_name = add_section_m.group(1).strip().title()
        print(f"[FAST] Adding new '{section_name}' section")
        new_sec = f"\n\\section{{{section_name}}}\n% Add your {section_name.lower()} here\n"
        if '\\end{document}' in modified:
            modified = modified.replace('\\end{document}', new_sec + '\\end{document}')
        else:
            modified += new_sec
        print(f"[FAST] Added section '{section_name}' instantly!")

    elif add_generic_m and not is_complex:
        raw_item = add_generic_m.group(1).strip()
        item = raw_item.title()
        print(f"[FAST] Generic add: inserting '{item}' into skills")
        modified, ok = insert_into_skills(modified, item)
        if not ok:
            print(f"[FAST] Could not locate skills line — NOT calling AI to avoid corruption")
            return {
                "latex_code": modified,
                "message": f"Could not find a skills/languages line to insert '{item}'. "
                           f"Please add it manually to your Technical Skills section.",
                "version": len(resumes[request.resume_id]['history']) - 1,
                "total_versions": len(resumes[request.resume_id]['history'])
            }

    else:
        print("[AI] Complex/creative instruction — using Ollama (may take 2–5 min)...")
        used_ai = True
        modified = await use_ai_edit(latex_code, request.instruction)

    if not used_ai:
        print("[FAST] Edit completed instantly (no AI used).")

    # Save to history
    resumes[request.resume_id]["history"].append({
        "latex": modified,
        "instruction": request.instruction,
        "timestamp": datetime.now().isoformat()
    })

    print(f"Modified length: {len(modified)} chars")
    print(f"{'='*60}\n")

    total = len(resumes[request.resume_id]["history"])
    return {"latex_code": modified, "message": "Done", "version": total - 1, "total_versions": total}

# ─── Undo (revert to previous version) ────────────────────────────────────────
@app.post("/undo/{resume_id}")
async def undo_resume(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    history = resumes[resume_id]["history"]
    if len(history) <= 1:
        raise HTTPException(400, "No previous version to undo to")
    history.pop()
    entry = history[-1]
    total = len(history)
    return {
        "latex_code": entry["latex"],
        "instruction": entry["instruction"],
        "version": total - 1,
        "total_versions": total,
        "message": f"Reverted to: {entry['instruction']}"
    }

# ─── History ───────────────────────────────────────────────────────────────────
@app.get("/history/{resume_id}")
async def get_history(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    history = resumes[resume_id]["history"]
    return {
        "resume_id": resume_id,
        "total_versions": len(history),
        "history": [
            {"version": i, "instruction": h["instruction"], "timestamp": h["timestamp"]}
            for i, h in enumerate(history)
        ]
    }

# ─── Reset to Original ─────────────────────────────────────────────────────────
@app.post("/reset/{resume_id}")
async def reset_resume(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    history = resumes[resume_id]["history"]
    original = history[0]
    resumes[resume_id]["history"] = [original]
    return {
        "latex_code": original["latex"],
        "version": 0,
        "total_versions": 1,
        "message": "Reset to original"
    }

# ─── Download .tex File ────────────────────────────────────────────────────────
@app.get("/download/{resume_id}")
async def download_resume(resume_id: str):
    if resume_id not in resumes:
        raise HTTPException(404, "Not found")
    history = resumes[resume_id]["history"]
    latex_code = history[-1]["latex"]
    filename = resumes[resume_id].get("filename", "resume.tex")
    if not filename.endswith(".tex"):
        filename = "resume.tex"

    # Write to a temp file and serve it
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tex", mode="w", encoding="utf-8")
    tmp.write(latex_code)
    tmp.close()

    return FileResponse(
        path=tmp.name,
        filename=filename,
        media_type="application/x-tex"
    )

# ─── AI Edit Function ──────────────────────────────────────────────────────────
async def use_ai_edit(latex_code: str, instruction: str) -> str:
    """Full Document AI Editing using local Ollama model."""

    print(f"\n{'='*60}")
    print(f"AI EDITING: {instruction}")
    print(f"{'='*60}")

    prompt = rf"""You are a professional LaTeX resume editor.
Your task: Modify the provided LaTeX code according to the user's instruction.

STRICT RULES:
1. Return the FULL document starting from \documentclass.
2. Maintain the EXACT existing formatting and all custom LaTeX macros.
3. Integrate the changes naturally and professionally.
4. Do NOT use "..." or summaries. Output every single line.
5. Do NOT add any explanation, commentary, or markdown — output ONLY the LaTeX code.

USER INSTRUCTION: {instruction}

ORIGINAL LATEX CODE:
{latex_code}
"""

    try:
        print("AI processing (this may take 2-4 mins for large resumes)...", end="", flush=True)
        response = ollama.generate(
            model='qwen2.5:3b',
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

        # Clean up markdown code blocks if AI wrapped in them
        if "```" in modified_code:
            match = re.search(r'```(?:latex)?\n?(.*?)\n?```', modified_code, re.DOTALL)
            if match:
                modified_code = match.group(1).strip()
            else:
                modified_code = '\n'.join([l for l in modified_code.split('\n') if "```" not in l]).strip()

        # Ensure only ONE document preamble
        if modified_code.count("\\documentclass") > 1:
            parts = modified_code.split("\\documentclass")
            modified_code = "\\documentclass" + parts[-1]

        # Fix common AI hallucinations
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

if __name__ == "__main__":
    import uvicorn
    print("\n===========================================")
    print("   AI RESUME EDITOR - READY")
    print("   Open: http://localhost:8000")
    print("===========================================\n")
    uvicorn.run(app, host="0.0.0.0", port=8000)