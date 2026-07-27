import os
import json
import sqlite3
import zipfile
import glob
import javalang
import argparse
from typing import List, Dict, Set, Optional, Tuple


class DiskCatalog:
    """Handles disk-backed storage using SQLite for zero-RAM overhead on 5000+ class monoliths."""

    def __init__(self, db_path: str = "analysis_db.sqlite", reset: bool = True):
        self.db_path = db_path
        if reset and os.path.exists(db_path):
            os.remove(db_path)

        self.conn = sqlite3.connect(self.db_path)
        # Performance optimizations for bulk inserts
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.execute("PRAGMA journal_mode = MEMORY")
        self._init_db()

    def _init_db(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS classes (
                    class_name TEXT PRIMARY KEY,
                    package TEXT,
                    is_dependency INTEGER,
                    dep_depth INTEGER,
                    annotations TEXT,
                    extends_class TEXT,
                    implements_json TEXT
                );

                CREATE TABLE IF NOT EXISTS fields (
                    class_name TEXT,
                    field_name TEXT,
                    field_type TEXT,
                    annotations TEXT
                );

                CREATE TABLE IF NOT EXISTS methods (
                    method_id TEXT PRIMARY KEY,
                    class_name TEXT,
                    method_name TEXT,
                    annotations TEXT
                );

                CREATE TABLE IF NOT EXISTS method_calls (
                    caller_method_id TEXT,
                    callee_qualifier TEXT,
                    callee_method TEXT
                );

                CREATE TABLE IF NOT EXISTS interfaces (
                    interface_name TEXT,
                    impl_class TEXT
                );

                CREATE TABLE IF NOT EXISTS beans (
                    bean_type TEXT,
                    concrete_class TEXT,
                    qualifier TEXT
                );

                -- Indexes for rapid chain traversal on disk
                CREATE INDEX IF NOT EXISTS idx_fields_lookup ON fields(class_name, field_name);
                CREATE INDEX IF NOT EXISTS idx_calls_caller ON method_calls(caller_method_id);
                CREATE INDEX IF NOT EXISTS idx_interfaces_name ON interfaces(interface_name);
                CREATE INDEX IF NOT EXISTS idx_beans_type ON beans(bean_type);
                CREATE INDEX IF NOT EXISTS idx_beans_qualifier ON beans(qualifier);
            """)

    def insert_class_batch(self, class_data: dict, is_dep: bool, dep_depth: int):
        cursor = self.conn.cursor()
        c_name = class_data['class_name']

        cursor.execute(
            "INSERT OR REPLACE INTO classes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c_name, class_data['package'], 1 if is_dep else 0, dep_depth,
             json.dumps(class_data['annotations']), class_data['extends'], json.dumps(class_data['implements']))
        )

        for impl in class_data['implements']:
            cursor.execute("INSERT INTO interfaces VALUES (?, ?)", (impl, c_name))

        for f_name, f_info in class_data['fields'].items():
            cursor.execute(
                "INSERT INTO fields VALUES (?, ?, ?, ?)",
                (c_name, f_name, f_info['type'], json.dumps(f_info['annotations']))
            )

        for m_name, m_info in class_data['methods'].items():
            m_id = f"{c_name}.{m_name}"
            cursor.execute(
                "INSERT OR REPLACE INTO methods VALUES (?, ?, ?, ?)",
                (m_id, c_name, m_name, json.dumps(m_info['annotations']))
            )
            for call in m_info['calls']:
                cursor.execute(
                    "INSERT INTO method_calls VALUES (?, ?, ?)",
                    (m_id, call['qualifier'], call['member'])
                )

    def commit(self):
        self.conn.commit()

    def resolve_beans(self):
        """Builds bean mappings connecting interfaces to concrete implementations across local & dep code."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT class_name, annotations, implements_json FROM classes")
        rows = cursor.fetchall()

        bean_annotations = {'Component', 'Service', 'Repository', 'Controller', 'RestController', 'Configuration',
                            'Bean'}

        for class_name, anns_str, impls_str in rows:
            anns = set(json.loads(anns_str))
            impls = json.loads(impls_str)

            # Check if class is a Spring Bean
            if anns.intersection(bean_annotations):
                # Extract Qualifier if present
                qualifier = None
                for a in anns:
                    if a.startswith("Qualifier:"):
                        qualifier = a.split(":", 1)[1]

                # Map concrete class to itself
                cursor.execute("INSERT INTO beans VALUES (?, ?, ?)", (class_name, class_name, qualifier))

                # Map implemented interfaces (local or from dependencies) to this concrete class
                for iface in impls:
                    cursor.execute("INSERT INTO beans VALUES (?, ?, ?)", (iface, class_name, qualifier))

        self.conn.commit()

    def get_entry_points(self, entry_annotations: List[str]) -> List[str]:
        cursor = self.conn.cursor()
        entry_ids = []
        cursor.execute("SELECT method_id, annotations FROM methods")
        for m_id, anns_str in cursor.fetchall():
            anns = json.loads(anns_str)
            if any(ea in anns for ea in entry_annotations):
                entry_ids.append(m_id)
        return entry_ids

    def resolve_callee_class(self, caller_class: str, qualifier: Optional[str]) -> Optional[str]:
        cursor = self.conn.cursor()

        if not qualifier or qualifier in ('this', 'super'):
            return caller_class

        # 1. Check if qualifier is a field in the caller class
        cursor.execute("SELECT field_type, annotations FROM fields WHERE class_name = ? AND field_name = ?",
                       (caller_class, qualifier))
        field_row = cursor.fetchone()

        if field_row:
            raw_type, f_anns_str = field_row
            f_anns = json.loads(f_anns_str)

            # Check for explicit @Qualifier on field
            field_qualifier = None
            for a in f_anns:
                if a.startswith("Qualifier:"):
                    field_qualifier = a.split(":", 1)[1]

            # Look up concrete bean by type and qualifier
            if field_qualifier:
                cursor.execute("SELECT concrete_class FROM beans WHERE bean_type = ? AND qualifier = ?",
                               (raw_type, field_qualifier))
            else:
                cursor.execute("SELECT concrete_class FROM beans WHERE bean_type = ?", (raw_type,))

            bean_row = cursor.fetchone()
            if bean_row:
                return bean_row[0]

            # Fallback to field raw type if no bean match
            return raw_type

        # 2. Check if qualifier directly references a known class/static call
        cursor.execute("SELECT class_name FROM classes WHERE class_name = ?", (qualifier,))
        cls_row = cursor.fetchone()
        if cls_row:
            return cls_row[0]

        return qualifier  # Fallback assuming qualifier is ClassName

    def get_outgoing_calls(self, method_id: str) -> List[Tuple[Optional[str], str]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT callee_qualifier, callee_method FROM method_calls WHERE caller_method_id = ?",
                       (method_id,))
        return cursor.fetchall()


class CodeFlowEngine:
    def __init__(self, args):
        self.args = args
        self.catalog = DiskCatalog(db_path=args.db, reset=True)
        self.parsed_classes: Set[str] = set()

    def parse_java_source(self, source_code: str) -> Optional[dict]:
        try:
            tree = javalang.parse.parse(source_code)
        except Exception:
            return None

        package = tree.package.name if tree.package else ""

        for _, node in tree.filter(javalang.tree.ClassDeclaration):
            class_name = node.name

            # Capture ALL annotations with properties
            annotations = []
            if node.annotations:
                for ann in node.annotations:
                    ann_str = ann.name
                    if hasattr(ann, 'element') and ann.element:
                        if isinstance(ann.element, javalang.tree.Literal):
                            ann_str += f":{ann.element.value.strip('\"')}"
                    annotations.append(ann_str)

            implements = [impl.name for impl in node.implements] if node.implements else []
            extends_class = node.extends.name if node.extends else None

            # Capture Fields & Field Annotations
            fields = {}
            for _, field in node.filter(javalang.tree.FieldDeclaration):
                f_type = field.type.name
                f_anns = []
                if field.annotations:
                    for ann in field.annotations:
                        a_str = ann.name
                        if hasattr(ann, 'element') and ann.element and isinstance(ann.element, javalang.tree.Literal):
                            a_str += f":{ann.element.value.strip('\"')}"
                        f_anns.append(a_str)

                for decl in field.declarators:
                    fields[decl.name] = {'type': f_type, 'annotations': f_anns}

            # Capture Methods & Outgoing Calls
            methods = {}
            for _, method in node.filter(javalang.tree.MethodDeclaration):
                m_anns = [ann.name for ann in method.annotations] if method.annotations else []
                calls = []
                for _, invocation in method.filter(javalang.tree.MethodInvocation):
                    calls.append({
                        'qualifier': invocation.qualifier,
                        'member': invocation.member
                    })
                methods[method.name] = {'annotations': m_anns, 'calls': calls}

            return {
                'package': package,
                'class_name': class_name,
                'annotations': annotations,
                'extends': extends_class,
                'implements': implements,
                'fields': fields,
                'methods': methods
            }
        return None

    def ingest_local_project(self):
        print(f"[1/4] Scanning local project: {self.args.src}")
        for root, _, files in os.walk(self.args.src):
            for file in files:
                if file.endswith(".java"):
                    path = os.path.join(root, file)
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        data = self.parse_java_source(f.read())
                        if data:
                            self.catalog.insert_class_batch(data, is_dep=False, dep_depth=0)
                            self.parsed_classes.add(data['class_name'])
        self.catalog.commit()

    def ingest_dependencies(self):
        if not self.args.gradle_cache or self.args.dep_depth <= 0:
            return

        print(f"[2/4] Recursively scanning dependencies (Max Depth: {self.args.dep_depth})...")
        search_pattern = os.path.join(self.args.gradle_cache, '**', '*-sources.jar')
        jar_files = glob.glob(search_pattern, recursive=True)

        for current_depth in range(1, self.args.dep_depth + 1):
            print(f"  -> Ingesting dependency tier {current_depth}...")
            for jar_path in jar_files:
                try:
                    with zipfile.ZipFile(jar_path, 'r') as zip_ref:
                        for name in zip_ref.namelist():
                            if name.endswith(".java"):
                                source_code = zip_ref.read(name).decode('utf-8', errors='ignore')
                                data = self.parse_java_source(source_code)
                                if data and data['class_name'] not in self.parsed_classes:
                                    self.catalog.insert_class_batch(data, is_dep=True, dep_depth=current_depth)
                                    self.parsed_classes.add(data['class_name'])
                except Exception:
                    continue
            self.catalog.commit()

    def generate_and_filter_chains(self):
        print("[3/4] Resolving Spring Beans & Cross-Boundary Interfaces on disk...")
        self.catalog.resolve_beans()

        print(f"[4/4] Traversing Call Graph on Disk (Filtering chains with length > {self.args.min_chain_length})...")
        entry_points = self.catalog.get_entry_points(
            ["RestController", "Controller", "Scheduled", "KafkaListener", "PostMapping", "GetMapping"]
        )

        chains_found = 0
        with open(self.args.out, 'w', encoding='utf-8') as out_file:
            out_file.write(f"=== Execution Chains (Min Length > {self.args.min_chain_length}) ===\n\n")

            for entry_id in entry_points:
                # Disk-assisted DFS stack to prevent memory stack overflow
                # Entry item: (current_node, path_history_list)
                stack = [(entry_id, [entry_id])]

                while stack:
                    curr_node, path = stack.pop()
                    caller_class, caller_method = curr_node.split('.', 1)

                    outgoing = self.catalog.get_outgoing_calls(curr_node)

                    # Filter out self-loops/cycles
                    valid_branches = []
                    for qualifier, member in outgoing:
                        callee_class = self.catalog.resolve_callee_class(caller_class, qualifier)
                        if callee_class:
                            callee_id = f"{callee_class}.{member}"
                            valid_branches.append(callee_id)

                    if not valid_branches:
                        # Leaf node reached: evaluate chain length requirement
                        # Length defined as number of execution steps in chain
                        if len(path) > self.args.min_chain_length:
                            chains_found += 1
                            chain_str = " ->\n    ".join(path)
                            output = f"Chain #{chains_found}:\n    {chain_str}\n\n"
                            out_file.write(output)
                            print(f"[Chain #{chains_found} | Length: {len(path)}]\n  " + " -> ".join(path))
                    else:
                        for callee_id in valid_branches:
                            if callee_id in path:  # Cycle detection
                                cycle_path = path + [f"[CYCLE: {callee_id}]"]
                                if len(cycle_path) > self.args.min_chain_length:
                                    chains_found += 1
                                    chain_str = " ->\n    ".join(cycle_path)
                                    out_file.write(f"Chain #{chains_found} (Cycle):\n    {chain_str}\n\n")
                            else:
                                stack.append((callee_id, path + [callee_id]))

        print(f"\nAnalysis Complete. {chains_found} chains written to {self.args.out}")

    def run(self):
        self.ingest_local_project()
        self.ingest_dependencies()
        self.generate_and_filter_chains()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Large-Scale Spring Boot Flow Engine")
    # parser.add_argument("--src", default="./src/main/java", help="Path to project Java source code")
    parser.add_argument("--src", default="F:\GitOthers\spring-petclinic\src\main\java", help="Path to project Java source code")
    parser.add_argument("--gradle-cache", default=os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/"),
                        help="Path to Gradle cache")
    parser.add_argument("--dep-depth", type=int, default=1, help="Max recursion depth into dependency JARs")
    parser.add_argument("--min-chain-length", type=int, default=2,
                        help="Only print/save chains longer than N steps (default: 3)")
    parser.add_argument("--db", default="analysis_db.sqlite", help="SQLite DB path for offloaded storage")
    parser.add_argument("--out", default="flow_chains.txt", help="Output text file for calculated chains")

    args = parser.parse_args()
    engine = CodeFlowEngine(args)
    engine.run()

