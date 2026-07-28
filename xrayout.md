Here is **Part 3**: the AI Enrichment, Persistence (Database), and Visibility Layer.

This layer integrates directly with the Python engine from Part 2. It reads the Java `.java` source files, generates context-aware prompts for each method in a call chain, persists AI responses in an **SQLite database** (acting as both a historical store and an LLM response cache to prevent redundant API calls), and exports the enriched chains into an **Interactive HTML Visualizer** and a **Markdown Document**.

---

## Part 3 Architecture Overview

```
                        ┌────────────────────────┐
                        │   Java Source Files    │
                        └───────────┬────────────┘
                                    │ (Extract Source)
                                    ▼
┌──────────────────┐    ┌────────────────────────┐    ┌──────────────────┐
│ CallTreeNode     ├───>│   AI Chain Enricher    ├───>│ SQLite DB        │
│ (from Part 2)    │    └───────────┬────────────┘    │ (Cache & Store)  │
└──────────────────┘                │                 └──────────────────┘
                                    │ (Enriched Tree)
                                    ▼
                       ┌──────────────────────────┐
                       │    Visibility Layer      │
                       ├──────────────────────────┤
                       │  • HTML Interactive View │
                       │  • Markdown Documentation│
                       └──────────────────────────┘

```

---

## Python Code Implementation (`ai_visibility_engine.py`)

Save this code alongside your `flow_engine.py` from Part 2.

```python
import os
import re
import json
import sqlite3
import hashlib
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict

# Import structures from Part 2
from flow_engine import SpringFlowAnalyzer, CallTreeNode, BeanNode


# =====================================================================
# 1. JAVA SOURCE CODE EXTRACTOR
# =====================================================================

class JavaSourceExtractor:
    """Extracts method source code from raw .java files using filesystem mapping."""
    
    def __init__(self, source_roots: List[str]):
        self.source_roots = [Path(p) for p in source_roots]

    def find_java_file(self, class_name: str) -> Optional[Path]:
        """Maps package class name (e.g., com.example.UserService) to actual file path."""
        rel_path = Path(*class_name.split('.')).with_suffix('.java')
        for root in self.source_roots:
            candidate = root / rel_path
            if candidate.exists():
                return candidate
        return None

    def extract_method_code(self, class_name: str, method_name: str) -> str:
        """Finds and extracts the exact body of a method from a Java class file."""
        file_path = self.find_java_file(class_name)
        if not file_path:
            return f"// Source code unavailable for class: {class_name}"

        try:
            content = file_path.read_text(encoding='utf-8')
            # Basic block matcher for method signatures
            pattern = re.compile(
                rf"(?:public|protected|private|static|\s)+[\w<>\[\]]+\s+{re.escape(method_name)}\s*\([^)]*\)\s*(?:throws\s+[\w,\s]+)?\s*\{",
                re.MULTILINE
            )
            match = pattern.search(content)
            if not match:
                return f"// Method '{method_name}' declaration not found in {file_path.name}"

            start_idx = match.start()
            # Bracket balancing to extract the full method body
            brace_count = 0
            end_idx = start_idx
            found_open = False

            for i in range(match.start(), len(content)):
                char = content[i]
                if char == '{':
                    brace_count += 1
                    found_open = True
                elif char == '}':
                    brace_count -= 1

                if found_open and brace_count == 0:
                    end_idx = i + 1
                    break

            return content[start_idx:end_idx].strip()
        except Exception as e:
            return f"// Error reading source for {class_name}.{method_name}: {str(e)}"


# =====================================================================
# 2. PERSISTENCE LAYER (SQLite DB)
# =====================================================================

class AnalysisDatabase:
    """Stores AI explanations and data flows. Serves as a cache to prevent duplicate LLM calls."""

    def __init__(self, db_path: str = "flow_analysis_cache.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS method_ai_cache (
                    method_key TEXT PRIMARY KEY,
                    source_hash TEXT,
                    summary TEXT,
                    data_flow_context TEXT,
                    raw_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    @staticmethod
    def generate_hash(class_name: str, method_name: str, source_code: str) -> str:
        data = f"{class_name}::{method_name}::{source_code}"
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def get_cached_analysis(self, method_key: str, source_hash: str) -> Optional[dict]:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT summary, data_flow_context, raw_json FROM method_ai_cache WHERE method_key = ? AND source_hash = ?",
                (method_key, source_hash)
            )
            row = cursor.fetchone()
            if row:
                return {
                    "summary": row[0],
                    "data_flow_context": row[1],
                    "raw": json.loads(row[2])
                }
        return None

    def save_analysis(self, method_key: str, source_hash: str, summary: str, data_flow_context: str, raw_json: dict):
        with self._get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO method_ai_cache 
                   (method_key, source_hash, summary, data_flow_context, raw_json) 
                   VALUES (?, ?, ?, ?, ?)""",
                (method_key, source_hash, summary, data_flow_context, json.dumps(raw_json))
            )
            conn.commit()


# =====================================================================
# 3. GENERIC AI CLIENT & PROMPT ENGINE
# =====================================================================

class GenericAIClient:
    """
    Plug-and-play AI interface.
    Replace the body of `_call_llm_api` with OpenAI, Anthropic, Ollama, LangChain, or custom endpoint.
    """

    def analyze_method(self, class_name: str, method_name: str, source_code: str, parent_context: str, children_signatures: List[str]) -> dict:
        prompt = self.build_prompt(class_name, method_name, source_code, parent_context, children_signatures)
        
        # Call generic AI execution logic
        raw_response = self._call_llm_api(prompt)
        
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError:
            return {
                "summary": raw_response,
                "parameter_data_flow": "Failed to parse structured JSON from AI response.",
                "side_effects": "Unknown"
            }

# =====================================================================

# 4. ENRICHMENT PIPELINE

# =====================================================================

class FlowEnricher:
"""Combines Extractor, AI Client, and DB Cache to enrich the call tree."""

```
def __init__(self, extractor: JavaSourceExtractor, ai_client: GenericAIClient, db: AnalysisDatabase):
    self.extractor = extractor
    self.ai_client = ai_client
    self.db = db

def enrich_tree(self, node: CallTreeNode, parent_summary: str = ""):
    method_key = f"{node.class_name}::{node.method_name}"
    
    # 1. Extract Source Code
    node.source_code = self.extractor.extract_method_code(node.class_name, node.method_name)
    source_hash = self.db.generate_hash(node.class_name, node.method_name, node.source_code)

    # 2. Check Database Cache
    cached = self.db.get_cached_analysis(method_key, source_hash)
    if cached:
        node.ai_explanation = cached["summary"]
        node.data_flow_context = cached["data_flow_context"]
    else:
        # 3. Call AI Engine if Cache Miss
        children_sigs = [f"{c.class_name}::{c.method_name}" for c in node.children]
        ai_res = self.ai_client.analyze_method(
            class_name=node.class_name,
            method_name=node.method_name,
            source_code=node.source_code,
            parent_context=parent_summary,
            children_signatures=children_sigs
        )
        
        node.ai_explanation = ai_res.get("summary", "")
        node.data_flow_context = ai_res.get("parameter_data_flow", "")

        # Save to SQLite DB
        self.db.save_analysis(
            method_key=method_key,
            source_hash=source_hash,
            summary=node.ai_explanation,
            data_flow_context=node.data_flow_context,
            raw_json=ai_res
        )

    # 4. Recursively enrich children
    for child in node.children:
        self.enrich_tree(child, parent_summary=node.ai_explanation)

```

# =====================================================================

# 5. VISIBILITY & PRESENTATION LAYER

# =====================================================================

class PresentationEngine:
"""Renders enriched trees into interactive HTML dashboard and Markdown files."""

```
@staticmethod
def render_markdown(root: CallTreeNode, output_file: str = "flow_documentation.md"):
    md_lines = ["# Application Code Flow Analysis\n"]

    def _build_md(node: CallTreeNode, depth: int = 0):
        indent = "  " * depth
        heading = "#" * min(depth + 2, 6)
        md_lines.append(f"{indent}- **`{node.class_name}::{node.method_name}`**")
        if node.ai_explanation:
            md_lines.append(f"{indent}  - **AI Overview:** {node.ai_explanation}")
        if node.data_flow_context:
            md_lines.append(f"{indent}  - **Data Flow:** {node.data_flow_context}")
        
        for child in node.children:
            _build_md(child, depth + 1)

    _build_md(root)
    Path(output_file).write_text("\n".join(md_lines), encoding='utf-8')
    print(f"Markdown report generated: {output_file}")

@staticmethod
def render_html_dashboard(root: CallTreeNode, output_file: str = "flow_dashboard.html"):
    """Generates a fully interactive standalone HTML view with expandable nodes."""

    def _tree_to_dict(node: CallTreeNode) -> dict:
        return {
            "className": node.class_name,
            "methodName": node.method_name,
            "summary": node.ai_explanation or "No explanation generated.",
            "dataFlow": node.data_flow_context or "N/A",
            "sourceCode": node.source_code or "",
            "children": [_tree_to_dict(c) for c in node.children]
        }

    tree_json = json.dumps(_tree_to_dict(root))

    html_content = f"""<!DOCTYPE html>

```

```
<script>
    const treeData = {tree_json};

    function renderTree(node) {{
        const li = document.createElement('li');
        const box = document.createElement('div');
        box.className = 'node-item';
        box.innerHTML = `<span class="class-name">${{node.className}}</span><span class="method-name">${{node.methodName}}()</span>`;
        box.onclick = (e) => {{ e.stopPropagation(); showDetail(node); }};
        li.appendChild(box);

        if (node.children && node.children.length > 0) {{
            const ul = document.createElement('ul');
            node.children.forEach(child => ul.appendChild(renderTree(child)));
            li.appendChild(ul);
        }}
        return li;
    }}

    function showDetail(node) {{
        const details = document.getElementById('detailContainer');
        details.innerHTML = `
            <h2 style="margin-top:0; color:#38bdf8;">${{node.className}}::${{node.methodName}}</h2>
            <div class="card">
                <div class="card-title">AI Summary</div>
                <div>${{node.summary}}</div>
            </div>
            <div class="card" style="border-left-color: #a855f7;">
                <div class="card-title">Data Flow & Parameter Transformations</div>
                <div>${{node.dataFlow}}</div>
            </div>
            <div class="card-title">Method Source Code</div>
            <pre><code>${{escapeHtml(node.sourceCode)}}</code></pre>
        `;
    }}

    function escapeHtml(text) {{
        return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }}

    const rootUl = document.createElement('ul');
    rootUl.style.paddingLeft = '0';
    rootUl.appendChild(renderTree(treeData));
    document.getElementById('treeContainer').appendChild(rootUl);
</script>

```

# =====================================================================

# MAIN RUNNER

# =====================================================================

if **name** == "**main**":
# 1. Initialize Part 2 Analyzer
flow_analyzer = SpringFlowAnalyzer("flow-analysis.json")

```
# 2. Define source code directory roots to extract Java files
# Add root paths for both src/main/java and extracted dependency sources
SOURCE_ROOTS = [
    "./src/main/java",
    "./src/test/java"
]

extractor = JavaSourceExtractor(source_roots=SOURCE_ROOTS)
ai_client = GenericAIClient()
db = AnalysisDatabase("flow_analysis_cache.db")
enricher = FlowEnricher(extractor, ai_client, db)

# 3. Detect root entry points and select a chain
roots = flow_analyzer.extract_entry_points()
if roots:
    start_class, start_method = roots[0]
    print(f"Building and enriching chain starting at: {start_class}::{start_method}")

    # Build execution tree
    chain_tree = flow_analyzer.build_call_chain(start_class, start_method)

    if chain_tree:
        # Enrich with AI & Source Code
        enricher.enrich_tree(chain_tree)

        # Render outputs
        PresentationEngine.render_markdown(chain_tree, "flow_documentation.md")
        PresentationEngine.render_html_dashboard(chain_tree, "flow_dashboard.html")
else:
    print("No root entry points identified in flow-analysis.json")

```

```

---

## How to Plug In Your Own AI Engine

Locate the `GenericAIClient` class in `ai_visibility_engine.py` and modify the `_call_llm_api` method to call your provider of choice:

#### Example: OpenAI API Integration
```python
import openai

def _call_llm_api(self, prompt: str) -> str:
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    return response.choices[0].message.content

```

#### Example: Ollama / Local LLM Integration

```python
import requests

def _call_llm_api(self, prompt: str) -> str:
    res = requests.post("http://localhost:11434/api/generate", json={
        "model": "codellama",
        "prompt": prompt,
        "stream": False
    })
    return res.json()["response"]

```

---

## Output Demonstration

When you run this pipeline:

1. **Source Code Extraction:** Finds the `.java` file, balances bracket delimiters `{ ... }`, and pulls the method body.
2. **Caching & DB:** Stores method summaries inside `flow_analysis_cache.db`. Subsequent runs reuse cached AI explanations if the underlying source code hash hasn't changed.
3. **Markdown Output (`flow_documentation.md`):** Creates structured technical docs suitable for Git repository wikis.
4. **Interactive Dashboard (`flow_dashboard.html`):** Open this file in any browser to get a clickable visual tree where selecting any method reveals its source code, AI summary, and data parameter transformations.