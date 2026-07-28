import json
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional


@dataclass
class MethodCall:
    target_class: str
    target_method: str
    signature: str


@dataclass
class BeanNode:
    bean_name: str
    class_name: str
    location: str
    dependencies: List[str]
    method_flows: Dict[str, List[MethodCall]]


@dataclass
class CallTreeNode:
    class_name: str
    method_name: str
    signature: str
    children: List['CallTreeNode'] = field(default_factory=list)

    # --- Part 3 AI Compatibility Hooks ---
    source_code: Optional[str] = None
    ai_explanation: Optional[str] = None
    data_flow_context: Optional[str] = None

    def print_tree(self, level=0):
        """Recursively prints the chain for verification."""
        indent = "  " * level
        node_str = f"{indent}-> {self.class_name}::{self.method_name}"

        # If AI data is present (for Part 3), print it
        if self.ai_explanation:
            node_str += f" [AI: {self.ai_explanation}]"

        print(node_str)
        for child in self.children:
            child.print_tree(level + 1)


class SpringFlowAnalyzer:
    def __init__(self, json_path: str):
        self.beans: Dict[str, BeanNode] = {}
        # Reverse lookup to find a bean by its class name
        self.class_to_bean: Dict[str, BeanNode] = {}
        self._load_data(json_path)

    def _load_data(self, json_path: str):
        with open(json_path, 'r') as f:
            raw_data = json.load(f)

        for bean_name, data in raw_data.items():
            bean = BeanNode(
                bean_name=data['beanName'],
                class_name=data['className'],
                location=data['location'],
                dependencies=data.get('dependencies', []),
                method_flows={
                    k: [MethodCall(**mc) for mc in v]
                    for k, v in data.get('methodFlows', {}).items()
                }
            )
            self.beans[bean_name] = bean
            self.class_to_bean[bean.class_name] = bean

    def resolve_target_class(self, caller_bean: BeanNode, target_class: str, target_method: str) -> str:
        """
        Attempts to resolve interfaces/abstract classes to their actual Spring Beans.
        Bytecode records the interface type, but we need the actual implementation
        injected by Spring to continue the chain accurately.
        """
        if target_class in self.class_to_bean:
            return target_class  # Exact match

        # Fallback heuristic: Check the caller's injected dependencies
        for dep_name in caller_bean.dependencies:
            if dep_name in self.beans:
                dep_bean = self.beans[dep_name]
                # If the injected bean implements the method we are calling, assume it's the target
                for method_key in dep_bean.method_flows.keys():
                    if method_key.startswith(target_method):
                        return dep_bean.class_name

        return target_class  # Return original if unresolved

    def build_call_chain(self, start_class: str, start_method: str) -> Optional[CallTreeNode]:
        """
        Builds a full execution tree from a starting method.
        Uses a visited set to detect and break circular dependencies (infinite loops).
        """
        visited = set()

        def traverse(current_class: str, current_method: str, signature: str = "") -> CallTreeNode:
            node = CallTreeNode(
                class_name=current_class,
                method_name=current_method,
                signature=signature
            )

            state_key = (current_class, current_method)
            if state_key in visited:
                node.ai_explanation = "Cycle detected, stopping traversal."
                return node

            visited.add(state_key)

            bean = self.class_to_bean.get(current_class)
            if not bean:
                visited.remove(state_key)
                return node

            # Find matching method in the bean's method_flows
            # (Handling cases where signature might be omitted in the starting request)
            target_key = next((k for k in bean.method_flows.keys() if k.startswith(current_method)), None)

            if target_key:
                calls = bean.method_flows[target_key]
                for call in calls:
                    actual_target_class = self.resolve_target_class(bean, call.target_class, call.target_method)
                    child_node = traverse(actual_target_class, call.target_method, call.signature)
                    node.children.append(child_node)

            visited.remove(state_key)
            return node

        return traverse(start_class, start_method)

    def extract_entry_points(self) -> List[tuple]:
        """
        Identifies potential starting points for chains (e.g., Controllers, Listeners).
        For simplicity, this example returns methods that aren't called by any other tracked bean.
        """
        all_called_methods = set()
        for bean in self.beans.values():
            for calls in bean.method_flows.values():
                for call in calls:
                    actual_class = self.resolve_target_class(bean, call.target_class, call.target_method)
                    all_called_methods.add((actual_class, call.target_method))

        entry_points = []
        for bean in self.beans.values():
            for method_sig in bean.method_flows.keys():
                method_name = method_sig.split('(')[0]
                if method_name != "<init>" and (bean.class_name, method_name) not in all_called_methods:
                    entry_points.append((bean.class_name, method_name))

        return entry_points


if __name__ == "__main__":
    analyzer = SpringFlowAnalyzer("flow-analysis.json")

    print("Detected Entry Points (Roots):")
    roots = analyzer.extract_entry_points()
    for cls, method in roots:
        print(f"- {cls}::{method}")

    print("\nExample Call Chain for the first entry point:")
    if roots:
        start_cls, start_method = roots[0]
        tree = analyzer.build_call_chain(start_cls, start_method)
        if tree:
            tree.print_tree()