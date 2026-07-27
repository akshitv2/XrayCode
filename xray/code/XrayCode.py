import os
import json
import javalang
from dataclasses import dataclass, asdict, field
from typing import List, Dict, Set, Optional


# --- Configuration ---
@dataclass
class Config:
    source_dirs: List[str] = field(default_factory=lambda: [
        "./src/main/java",
        os.path.expanduser("~/.gradle/caches/modules-2/files-2.1")  # Requires extracted -sources.jar
    ])
    output_catalog: str = "class_catalog.json"
    output_chains: str = "flow_chains.txt"
    spring_annotations: Set[str] = field(default_factory=lambda: {
        "Component", "Service", "Repository", "Controller", "RestController",
        "Configuration", "Bean"
    })
    inject_annotations: Set[str] = field(default_factory=lambda: {
        "Autowired", "Inject", "Resource"
    })


# --- Data Models ---
@dataclass
class MethodDef:
    name: str
    return_type: str
    parameters: List[str]
    calls: List['MethodCall'] = field(default_factory=list)


@dataclass
class MethodCall:
    target_instance: str  # e.g., 'userService'
    target_method: str  # e.g., 'getUser'


@dataclass
class ClassDef:
    name: str
    package: str
    is_interface: bool
    super_class: Optional[str]
    implements: List[str]
    annotations: List[str]
    fields: Dict[str, str] = field(default_factory=dict)  # name -> type
    methods: Dict[str, MethodDef] = field(default_factory=dict)
    is_bean: bool = False


# --- Engine ---
class SpringFlowAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.catalog: Dict[str, ClassDef] = {}  # FQN -> ClassDef
        self.inheritance_map: Dict[str, List[str]] = {}  # Parent -> [Children FQNs]
        self.beans: Dict[str, str] = {}  # Bean Type -> FQN (Simplified for primary candidates)

    def run(self):
        self.scan_and_catalog()
        self.resolve_inheritance()
        self.resolve_beans()
        self.dump_catalog()
        chains = self.build_call_chains()
        self.dump_chains(chains)

    def scan_and_catalog(self):
        for directory in self.config.source_dirs:
            if not os.path.exists(directory):
                continue
            for root, _, files in os.walk(directory):
                for file in files:
                    if file.endswith(".java"):
                        self._parse_file(os.path.join(root, file))

    def _parse_file(self, filepath: str):
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            tree = javalang.parse.parse(content)

            package_name = tree.package.name if tree.package else ""

            for path, node in tree.filter(javalang.tree.TypeDeclaration):
                fqn = f"{package_name}.{node.name}" if package_name else node.name

                is_interface = isinstance(node, javalang.tree.InterfaceDeclaration)
                super_class = node.extends.name if hasattr(node, 'extends') and node.extends else None
                implements = [impl.name for impl in node.implements] if hasattr(node,
                                                                                'implements') and node.implements else []

                annotations = [ann.name for ann in node.annotations] if hasattr(node, 'annotations') else []
                is_bean = any(ann in self.config.spring_annotations for ann in annotations)

                class_def = ClassDef(
                    name=node.name,
                    package=package_name,
                    is_interface=is_interface,
                    super_class=super_class,
                    implements=implements,
                    annotations=annotations,
                    is_bean=is_bean
                )

                # Parse Fields (for DI via @Autowired)
                for _, field_node in node.filter(javalang.tree.FieldDeclaration):
                    field_type = field_node.type.name
                    for declarator in field_node.declarators:
                        class_def.fields[declarator.name] = field_type

                # Parse Constructors (for Constructor DI)
                for _, constr_node in node.filter(javalang.tree.ConstructorDeclaration):
                    for param in constr_node.parameters:
                        class_def.fields[param.name] = param.type.name

                # Parse Methods and Calls
                for _, method_node in node.filter(javalang.tree.MethodDeclaration):
                    method_name = method_node.name
                    return_type = method_node.return_type.name if method_node.return_type else "void"
                    params = [p.type.name for p in method_node.parameters]

                    method_def = MethodDef(method_name, return_type, params)

                    # Track @Bean methods in @Configuration classes
                    if "Configuration" in annotations and hasattr(method_node, 'annotations'):
                        if any(ann.name == "Bean" for ann in method_node.annotations):
                            # The return type is registered as a bean
                            self.beans[return_type] = fqn  # Note: actual bean is the return type, simplified map

                    # Find method invocations inside this method
                    for _, invoc_node in method_node.filter(javalang.tree.MethodInvocation):
                        if invoc_node.qualifier:  # e.g., `userService.getUser()` -> qualifier is `userService`
                            method_def.calls.append(MethodCall(invoc_node.qualifier, invoc_node.member))

                    class_def.methods[method_name] = method_def

                self.catalog[fqn] = class_def
        except Exception as e:
            # Skip unparseable files or syntax errors
            pass

    def resolve_inheritance(self):
        for fqn, class_def in self.catalog.items():
            parents = class_def.implements + ([class_def.super_class] if class_def.super_class else [])
            for parent in parents:
                if parent not in self.inheritance_map:
                    self.inheritance_map[parent] = []
                self.inheritance_map[parent].append(fqn)

    def resolve_beans(self):
        for fqn, class_def in self.catalog.items():
            if class_def.is_bean:
                self.beans[class_def.name] = fqn

    def dump_catalog(self):
        with open(self.config.output_catalog, 'w', encoding='utf-8') as f:
            json.dump({k: asdict(v) for k, v in self.catalog.items()}, f, indent=4)

    def build_call_chains(self) -> List[List[str]]:
        chains = []
        # Find entry points (e.g., Controllers)
        entry_classes = [fqn for fqn, cls in self.catalog.items() if
                         "Controller" in cls.annotations or "RestController" in cls.annotations]

        for entry in entry_classes:
            cls_def = self.catalog[entry]
            for method_name, method_def in cls_def.methods.items():
                # Start DFS
                self._dfs_chain(entry, method_name, [], chains, set())

        return chains

    def _dfs_chain(self, current_fqn: str, current_method: str, current_chain: List[str], all_chains: List[List[str]],
                   visited: Set[str]):
        chain_node = f"{current_fqn}.{current_method}()"

        if chain_node in visited:
            all_chains.append(current_chain + [f"{chain_node} (CYCLE)"])
            return

        visited.add(chain_node)
        new_chain = current_chain + [chain_node]

        cls_def = self.catalog.get(current_fqn)
        if not cls_def or current_method not in cls_def.methods:
            all_chains.append(new_chain)
            visited.remove(chain_node)
            return

        calls = cls_def.methods[current_method].calls
        if not calls:
            all_chains.append(new_chain)
        else:
            for call in calls:
                # 1. Map instance variable (qualifier) to Type
                target_type_short = cls_def.fields.get(call.target_instance)
                if not target_type_short:
                    continue  # Local variable or static call not tracked in fields

                # 2. Resolve Type to Implementation FQN
                target_fqn = self._resolve_target_implementation(target_type_short)
                if target_fqn:
                    self._dfs_chain(target_fqn, call.target_method, new_chain, all_chains, visited)

        visited.remove(chain_node)

    def _resolve_target_implementation(self, type_short_name: str) -> Optional[str]:
        # 1. Direct Bean Match
        if type_short_name in self.beans:
            return self.beans[type_short_name]

        # 2. Interface Resolution
        if type_short_name in self.inheritance_map:
            implementations = self.inheritance_map[type_short_name]
            for impl_fqn in implementations:
                if self.catalog.get(impl_fqn, ClassDef("", "", False, None, [], [])).is_bean:
                    return impl_fqn  # Return first Spring-managed implementation

            if implementations:
                return implementations[0]  # Fallback to first non-bean impl

        # 3. Direct catalog lookup (assuming no package collisions for simplicity)
        for fqn in self.catalog.keys():
            if fqn.endswith(f".{type_short_name}"):
                return fqn

        return None

    def dump_chains(self, chains: List[List[str]]):
        with open(self.config.output_chains, 'w', encoding='utf-8') as f:
            for chain in chains:
                f.write(" -> ".join(chain) + "\n")


if __name__ == "__main__":
    cfg = Config()
    cfg.source_dirs = "F:\GitOthers\spring-petclinic\src\main\java"
    analyzer = SpringFlowAnalyzer(cfg)
    analyzer.run()