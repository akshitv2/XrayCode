import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.aop.support.AopUtils;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.beans.factory.config.ConfigurableListableBeanFactory;
import org.springframework.context.ApplicationContext;
import org.springframework.context.ApplicationListener;
import org.springframework.context.event.ContextRefreshedEvent;
import org.springframework.stereotype.Component;
import org.springframework.asm.ClassReader;
import org.springframework.asm.ClassVisitor;
import org.springframework.asm.MethodVisitor;
import org.springframework.asm.Opcodes;

import java.io.File;
import java.io.InputStream;
import java.security.CodeSource;
import java.util.*;

@Component
public class SpringFlowAnalyzer implements ApplicationListener<ContextRefreshedEvent> {

    private static final Logger log = LoggerFactory.getLogger(SpringFlowAnalyzer.class);

    // Requirement 8: Make things configurable
    @Value("${flow.analysis.enabled:true}")
    private boolean enabled;

    @Value("${flow.analysis.base-packages:com.yourcompany}") // Change this to your root package
    private List<String> basePackages;

    @Value("${flow.analysis.output-file:flow-analysis.json}")
    private String outputFile;

    @Override
    public void onApplicationEvent(ContextRefreshedEvent event) {
        if (!enabled) return;

        log.info("Starting Flow Analysis Engine...");
        ApplicationContext context = event.getApplicationContext();
        ConfigurableListableBeanFactory factory = (ConfigurableListableBeanFactory) context.getAutowireCapableBeanFactory();

        Map<String, BeanNode> database = new HashMap<>();

        // Requirement 1 & 7: Catalogue classes (including dependencies that are instantiated as beans)
        String[] beanNames = factory.getBeanDefinitionNames();

        for (String beanName : beanNames) {
            Object bean = null;
            try {
                bean = context.getBean(beanName);
            } catch (Exception e) {
                continue; // Skip abstract beans or those failing to initialize
            }

            // Requirement 4: Understand inheritance & proxies (unwrap CGLIB/JDK proxies to get actual class)
            Class<?> targetClass = AopUtils.getTargetClass(bean);

            // Filter by base package to avoid analyzing all of Spring Framework / Java base classes
            if (!isInTargetPackages(targetClass.getName())) {
                continue;
            }

            BeanNode node = new BeanNode();
            node.beanName = beanName;
            node.className = targetClass.getName();

            // Requirement 3: Original class file location
            node.location = getClassLocation(targetClass);

            // Requirement 2: Identify bean wirings/dependencies
            String[] dependencies = factory.getDependenciesForBean(beanName);
            node.dependencies.addAll(Arrays.asList(dependencies));

            // Requirement 5, 6, & 9: Analyze bytecode for method-to-method calls
            analyzeMethodCalls(targetClass, node);

            database.put(beanName, node);
        }

        // Export to JSON
        try {
            ObjectMapper mapper = new ObjectMapper();
            mapper.writerWithDefaultPrettyPrinter().writeValue(new File(outputFile), database);
            log.info("Flow analysis complete. Data written to {}", outputFile);
        } catch (Exception e) {
            log.error("Failed to write flow analysis JSON", e);
        }
    }

    private void analyzeMethodCalls(Class<?> clazz, BeanNode node) {
        try {
            String resourcePath = "/" + clazz.getName().replace('.', '/') + ".class";
            InputStream is = clazz.getResourceAsStream(resourcePath);
            if (is == null) return;

            ClassReader reader = new ClassReader(is);

            // Using Spring's repackaged ASM Opcodes
            reader.accept(new ClassVisitor(Opcodes.ASM9) {
                @Override
                public MethodVisitor visitMethod(int access, String name, String descriptor, String signature, String[] exceptions) {
                    // Requirement 9: Catalogue which method we are inside
                    List<MethodCall> calls = new ArrayList<>();
                    node.methodFlows.put(name + descriptor, calls);

                    return new MethodVisitor(Opcodes.ASM9) {
                        @Override
                        public void visitMethodInsn(int opcode, String owner, String calledMethodName, String calledMethodDescriptor, boolean isInterface) {
                            // Requirement 6: By scanning bytecode directly, we naturally traverse ALL branches
                            // (if-statements, switches, loops) because bytecode is linear. Every method invocation is captured.
                            String ownerClass = owner.replace('/', '.');
                            if (isInTargetPackages(ownerClass)) {
                                calls.add(new MethodCall(ownerClass, calledMethodName, calledMethodDescriptor));
                            }
                        }
                    };
                }
            }, ClassReader.SKIP_DEBUG | ClassReader.SKIP_FRAMES);

        } catch (Exception e) {
            log.warn("Could not analyze bytecode for class: " + clazz.getName(), e);
        }
    }

    private String getClassLocation(Class<?> clazz) {
        try {
            CodeSource codeSource = clazz.getProtectionDomain().getCodeSource();
            if (codeSource != null && codeSource.getLocation() != null) {
                return codeSource.getLocation().toString();
            }
        } catch (SecurityException e) {
            // Ignore module restrictions in modern Java
        }
        return "Unknown";
    }

    private boolean isInTargetPackages(String className) {
        if (className == null) return false;
        for (String pkg : basePackages) {
            if (className.startsWith(pkg)) return true;
        }
        return false;
    }

    // --- DTOs for JSON Serialization ---

    public static class BeanNode {
        public String beanName;
        public String className;
        public String location;
        public Set<String> dependencies = new HashSet<>();
        public Map<String, List<MethodCall>> methodFlows = new HashMap<>();
    }

    public static class MethodCall {
        public String targetClass;
        public String targetMethod;
        public String signature;

        public MethodCall(String targetClass, String targetMethod, String signature) {
            this.targetClass = targetClass;
            this.targetMethod = targetMethod;
            this.signature = signature;
        }
    }
}

/*
# Enable or disable the analyzer
flow.analysis.enabled=true
# Limit the scan to your company's code and specific dependency packages you care about.
# Scanning "java." or "org.springframework." will create massive overhead.
flow.analysis.base-packages=com.yourcompany,com.some.dependency.you.want.tracked
# Where to drop the payload for Python
flow.analysis.output-file=target/flow-analysis.json
*/