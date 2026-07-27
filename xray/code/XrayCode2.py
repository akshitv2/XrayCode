import os
import json
import zipfile
import glob
import javalang
import argparse
from collections import defaultdict
from typing import Dict, List, Set


class Config:
    def __init__(self, src_dir, gradle_cache_dir, output_file, entry_points):
        self.src_dir = src_dir
        self.gradle_cache_dir = gradle_cache_dir
        self.output_file = output_file
        # Entry points (e.g. Controller methods) to start tracking chains
        self.entry_points = entry_points or ["@RestController", "@Controller", "@Scheduled"]


class CodeFlowAnalyzer:
    def __init__(self, config: Config):
        self.config = config

        # Catalogs
        self.classes: Dict[str, dict] = {}  # ClassName -> Class details
        self.interfaces: Dict[str, list] = defaultdict(list)  # InterfaceName -> [Implementing Classes]

        # Bean Resolution
        self.bean_registry: Dict[str, str] = {}  # InjectedType -> Concrete Implementation

        # Call Graph
        self.call_graph: Dict[str, List[str]] = defaultdict(list)  # Class.method -> [Class.method, ...]

    def run(self):
        print("[1/5] Scanning local source files...")
        self._scan_directory(self.config.src_dir)

        if self.config.gradle_cache_dir and os.path.exists(self.config.gradle_cache_dir):
            print("[2/5] Scanning Gradle cache for source jars...")
            self._scan_gradle_cache(self.config.gradle_cache_dir)

        print("[3/5] Resolving Spring Beans and Inheritance...")
        self._resolve_beans()

        print("[4/5] Building Call Graph & App Flow...")
        self._build_call_graph()

        print(f"[5/5] Generating chains and saving to {self.config.output_file}...")
        self._export_catalog()
        chains = self._generate_chains()
        self._print_chains(chains)

    def _scan_directory(self, directory: str):
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".java"):
                    self._parse_java_file(os.path.join(root, file))

    def _scan_gradle_cache(self, cache_dir: str):
        # Find all *-sources.jar in the gradle cache
        search_pattern = os.path.join(cache_dir, '**', '*-sources.jar')
        for jar_path in glob.glob(search_pattern, recursive=True):
            try:
                with zipfile.ZipFile(jar_path, 'r') as zip_ref:
                    for name in zip_ref.namelist():
                        if name.endswith(".java"):
                            source_code = zip_ref.read(name).decode('utf-8', errors='ignore')
                            self._parse_java_string(source_code, name)
            except Exception as e:
                pass  # Skip corrupted or unreadable jars

    def _parse_java_file(self, filepath: str):
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            self._parse_java_string(f.read(), filepath)

    def _parse_java_string(self, source_code: str, source_name: str):
        try:
            tree = javalang.parse.parse(source_code)
        except javalang.parser.JavaSyntaxError:
            return  # Skip files that cannot be parsed

        for _, node in tree.filter(javalang.tree.ClassDeclaration):
            class_name = node.name
            annotations = [ann.name for ann in node.annotations] if node.annotations else []

            # Inheritance
            implements = [impl.name for impl in node.implements] if node.implements else []
            for iface in implements:
                self.interfaces[iface].append(class_name)

            # Fields (for Autowired / Injection)
            fields = {}
            for _, field in node.filter(javalang.tree.FieldDeclaration):
                field_type = field.type.name
                is_autowired = any(ann.name == 'Autowired' for ann in (field.annotations or []))
                for declarator in field.declarators:
                    fields[declarator.name] = {
                        'type': field_type,
                        'autowired': is_autowired
                    }

            # Constructors (for Constructor Injection)
            for _, constructor in node.filter(javalang.tree.ConstructorDeclaration):
                for param in constructor.parameters:
                    fields[param.name] = {'type': param.type.name, 'autowired': True}  # Assume injected

            # Methods and Calls
            methods = {}
            for _, method in node.filter(javalang.tree.MethodDeclaration):
                method_name = method.name
                method_anns = [ann.name for ann in method.annotations] if method.annotations else []
                calls = []
                for _, invocation in method.filter(javalang.tree.MethodInvocation):
                    calls.append({
                        'qualifier': invocation.qualifier,  # e.g., 'userService' in userService.findById()
                        'member': invocation.member  # e.g., 'findById'
                    })
                methods[method_name] = {'annotations': method_anns, 'calls': calls}

            self.classes[class_name] = {
                'source': source_name,
                'annotations': annotations,
                'implements': implements,
                'fields': fields,
                'methods': methods
            }

    def _resolve_beans(self):
        # Identify concrete implementations for beans
        for class_name, details in self.classes.items():
            is_bean = any(ann in details['annotations'] for ann in
                          ['Component', 'Service', 'Repository', 'Controller', 'RestController', 'Configuration'])
            if is_bean:
                # Register self
                self.bean_registry[class_name] = class_name
                # Register interfaces to this concrete class (handling simple inheritance)
                for iface in details['implements']:
                    # If multiple classes implement this, in a real Spring app a @Qualifier is used.
                    # Here we map to the first encountered for simplicity, but track it.
                    self.bean_registry[iface] = class_name

    def _build_call_graph(self):
        for class_name, details in self.classes.items():
            for method_name, method_details in details['methods'].items():
                caller_id = f"{class_name}.{method_name}"

                for call in method_details['calls']:
                    qualifier = call['qualifier']
                    callee_method = call['member']

                    if not qualifier:
                        # Internal method call
                        callee_class = class_name
                    else:
                        # Find the type of the variable being called
                        field_info = details['fields'].get(qualifier)
                        if field_info:
                            raw_type = field_info['type']
                            # Resolve the interface to a concrete bean using the registry
                            callee_class = self.bean_registry.get(raw_type, raw_type)
                        else:
                            # If it's a static call or local variable not tracked in fields
                            callee_class = qualifier

                    callee_id = f"{callee_class}.{callee_method}"
                    self.call_graph[caller_id].append(callee_id)

    def _generate_chains(self) -> List[List[str]]:
        chains = []
        visited_in_path = set()

        def dfs(current_node: str, current_chain: List[str]):
            # Cycle detection
            if current_node in visited_in_path:
                chains.append(list(current_chain) + [f"[CYCLE DETECTED: {current_node}]"])
                return

            # Leaf node detection (no further calls mapped)
            if current_node not in self.call_graph or not self.call_graph[current_node]:
                chains.append(list(current_chain))
                return

            visited_in_path.add(current_node)
            for neighbor in self.call_graph[current_node]:
                current_chain.append(neighbor)
                dfs(neighbor, current_chain)
                current_chain.pop()
            visited_in_path.remove(current_node)

        # Start DFS from entry points (e.g., Controllers)
        for class_name, details in self.classes.items():
            is_entry = any(ann in details['annotations'] for ann in self.config.entry_points)
            if is_entry:
                for method_name in details['methods'].keys():
                    start_node = f"{class_name}.{method_name}"
                    dfs(start_node, [start_node])

        return chains

    def _export_catalog(self):
        with open('class_catalog.json', 'w', encoding='utf-8') as f:
            json.dump(self.classes, f, indent=2)

    def _print_chains(self, chains: List[List[str]]):
        with open(self.config.output_file, 'w', encoding='utf-8') as f:
            f.write("=== Application Flow Chains ===\n\n")
            for i, chain in enumerate(chains, 1):
                chain_str = " ->\n    ".join(chain)
                output = f"Chain {i}:\n    {chain_str}\n"
                f.write(output + "\n")
                print(output)


# if __name__ == "__main__":
#     parser = argparse.ArgumentParser(description="Spring Boot Code Flow Analyzer")
#     parser.add_argument("--src", default="./src/main/java", help="Path to Java source directory")
#     parser.add_argument("--cache", default=os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/"),
#                         help="Path to Gradle cache")
#     parser.add_argument("--out", default="flow_chains.txt", help="Output file for chains")
#     args = parser.parse_args()
#
#     config = Config(
#         src_dir=args.src,
#         gradle_cache_dir=args.cache,
#         output_file=args.out,
#         entry_points=["RestController", "Controller", "Scheduled", "KafkaListener"]  # Spring entry points
#     )
#
#     analyzer = CodeFlowAnalyzer(config)
#     analyzer.run()
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Spring Boot Code Flow Analyzer")
    parser.add_argument("--src", default="./src/main/java", help="Path to Java source directory")
    parser.add_argument("--cache", default=os.path.expanduser("~/.gradle/caches/modules-2/files-2.1/"),
                        help="Path to Gradle cache")
    parser.add_argument("--out", default="flow_chains.txt", help="Output file for chains")
    args = parser.parse_args()

    config = Config(
        src_dir="F:\GitOthers\spring-petclinic\src\main\java",
        gradle_cache_dir=args.cache,
        output_file=args.out,
        entry_points=["RestController", "Controller", "Scheduled", "KafkaListener","VetController"]  # Spring entry points
    )

    analyzer = CodeFlowAnalyzer(config)
    analyzer.run()