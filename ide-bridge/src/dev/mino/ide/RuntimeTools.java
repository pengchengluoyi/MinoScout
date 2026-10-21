package dev.mino.ide;

import com.intellij.execution.ExecutionManager;
import com.intellij.execution.RunManager;
import com.intellij.execution.RunnerAndConfigurationSettings;
import com.intellij.execution.executors.DefaultRunExecutor;
import com.intellij.execution.impl.ExecutionManagerImpl;
import com.intellij.execution.process.ProcessHandler;
import com.intellij.execution.runners.ExecutionEnvironment;
import com.intellij.execution.runners.ExecutionEnvironmentBuilder;
import com.intellij.execution.runners.ProgramRunner;
import com.intellij.execution.ui.RunContentDescriptor;
import com.intellij.mcpserver.*;
import com.intellij.mcpserver.util.Execution_utilKt;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.application.ModalityState;
import com.intellij.openapi.project.Project;
import kotlin.ResultKt;
import kotlin.Unit;
import kotlin.coroutines.Continuation;
import kotlin.coroutines.CoroutineContext;
import kotlin.coroutines.intrinsics.IntrinsicsKt;
import kotlinx.serialization.json.*;
import java.nio.file.Path;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.Supplier;

public final class RuntimeTools implements McpToolsProvider {
    private static final ConcurrentMap<String, Restart> OPERATIONS = new ConcurrentHashMap<>();
    private static final ConcurrentMap<String, String> ACTIVE = new ConcurrentHashMap<>();

    @Override public List<McpTool> getTools() { return List.of(new Tool(false), new Tool(true)); }

    private static final class Restart {
        final String key;
        final String id;
        volatile String state = "queued";
        volatile String reason = "IDE 已接受重启请求";
        volatile List<Long> before = List.of();
        volatile long executionId;
        Restart(String key, String id) { this.key = key; this.id = id; }
        JsonObject json() { return object(Map.of("operation_id", id, "state", state, "reason", reason,
                "previous_execution_ids", before, "execution_id", executionId)); }
    }

    private static final class Tool implements McpTool {
        final boolean restart;
        final McpToolDescriptor descriptor;
        Tool(boolean restart) {
            this.restart = restart;
            String name = restart ? "mino_restart_run_configuration" : "mino_get_run_configuration_state";
            Map<String, JsonElement> properties = new LinkedHashMap<>();
            for (String field : List.of("projectPath", "configurationName", "operationId")) properties.put(field, object(Map.of("type", "string")));
            McpToolSchema schema = new McpToolSchema(new JsonObject(properties), restart ? Set.of("projectPath", "configurationName", "operationId") : Set.of("projectPath", "configurationName"), Map.of(), McpToolSchema.DEFAULT_DEFINITIONS_PATH);
            descriptor = new McpToolDescriptor(name, restart ? "Restart the exact saved configuration through the IDE. Return an idempotent operation receipt; never execute shell commands." : "Read the exact saved configuration runtime and optional restart receipt.",
                    new McpToolCategory("dev.mino.ide", "Mino Runtime"), "dev.mino.ide." + name, schema, null);
        }
        @Override public McpToolDescriptor getDescriptor() { return descriptor; }
        @Override public Object call(JsonObject arguments, Continuation<? super McpToolCallResult> continuation) {
            try {
                Project project = McpCallInfoKt.getProject(continuation.getContext());
                String path = text(arguments, "projectPath");
                String name = text(arguments, "configurationName");
                String id = optional(arguments, "operationId");
                if (project.isDisposed() || project.getBasePath() == null || !Path.of(project.getBasePath()).toRealPath().equals(Path.of(path).toRealPath())) throw new IllegalArgumentException("IDE 项目与请求不匹配");
                RunnerAndConfigurationSettings settings = edt(() -> configuration(project, name));
                if (!restart) return result(snapshot(project, settings, id), false);
                if (!id.matches("[A-Za-z0-9_-]{8,100}")) throw new IllegalArgumentException("缺少稳定 operationId");
                Continuation<Unit> permission = new Continuation<>() {
                    @Override public CoroutineContext getContext() { return continuation.getContext(); }
                    @Override public void resumeWith(Object value) {
                        try { ResultKt.throwOnFailure(value); continuation.resumeWith(request(project, settings, id)); }
                        catch (Throwable error) { continuation.resumeWith(result(object(Map.of("reason", safe(error))), true)); }
                    }
                };
                Object allowed = Execution_utilKt.checkUserConfirmationIfNeeded("Mino 重启运行配置", "重启「" + name + "」", project, permission);
                return allowed == IntrinsicsKt.getCOROUTINE_SUSPENDED() ? allowed : request(project, settings, id);
            } catch (Throwable error) { return result(object(Map.of("reason", safe(error))), true); }
        }
    }

    private static McpToolCallResult request(Project project, RunnerAndConfigurationSettings settings, String id) {
        String key = project.getLocationHash() + ":" + settings.getUniqueID();
        Restart operation;
        synchronized (ACTIVE) {
            Restart previous = OPERATIONS.get(id);
            if (previous != null) return result(previous.json(), !previous.key.equals(key));
            if (OPERATIONS.size() >= 4096) return result(object(Map.of("reason", "IDE 重启回执已达上限，未重复执行")), true);
            if (ACTIVE.putIfAbsent(key, id) != null) return result(object(Map.of("reason", "该配置已有重启操作")), true);
            operation = new Restart(key, id);
            OPERATIONS.put(id, operation);
        }
        ApplicationManager.getApplication().executeOnPooledThread(() -> restart(project, settings, operation));
        return result(operation.json(), false);
    }

    private static void restart(Project project, RunnerAndConfigurationSettings settings, Restart operation) {
        try {
            List<RunContentDescriptor> descriptors = edt(() -> running(project, settings));
            operation.before = descriptors.stream().map(RunContentDescriptor::getExecutionId).toList();
            List<ProcessHandler> handlers = descriptors.stream().map(RunContentDescriptor::getProcessHandler).filter(Objects::nonNull).toList();
            operation.state = "stopping";
            operation.reason = "IDE 正在停止当前配置";
            edt(() -> { for (ProcessHandler handler : handlers) if (!handler.isProcessTerminated()) ExecutionManagerImpl.stopProcess(handler); return null; });
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(25);
            for (ProcessHandler handler : handlers) while (!handler.waitFor(200)) if (project.isDisposed() || System.nanoTime() >= deadline) throw new IllegalStateException("旧实例尚未退出，未启动重复实例");
            operation.state = "starting";
            operation.reason = "IDE 正在重新启动当前配置";
            CompletableFuture<RunContentDescriptor> started = new CompletableFuture<>();
            edt(() -> {
                if (!running(project, settings).isEmpty()) throw new IllegalStateException("该配置已被其他操作启动，未重复启动");
                try {
                    ExecutionEnvironment environment = ExecutionEnvironmentBuilder.create(DefaultRunExecutor.getRunExecutorInstance(), settings).build(new ProgramRunner.Callback() {
                        @Override public void processStarted(RunContentDescriptor descriptor) { started.complete(descriptor); }
                        @Override public void processNotStarted() { started.completeExceptionally(new IllegalStateException("IDE 未启动该配置")); }
                        @Override public void processNotStarted(Throwable error) { started.completeExceptionally(error); }
                    });
                    environment.assignNewExecutionId();
                    environment.getRunner().execute(environment);
                } catch (Exception error) { throw new IllegalStateException("IDE 无法启动该配置", error); }
                return null;
            });
            RunContentDescriptor descriptor = started.get(60, TimeUnit.SECONDS);
            operation.executionId = descriptor.getExecutionId();
            if (descriptor.getProcessHandler() == null || descriptor.getProcessHandler().isProcessTerminated()) throw new IllegalStateException("新实例已退出，请查看运行输出");
            operation.state = "ready";
            operation.reason = "IDE 已停止旧实例并启动新实例";
        } catch (Throwable error) { operation.state = "failed"; operation.reason = safe(error); }
        finally { ACTIVE.remove(operation.key, operation.id); }
    }

    private static JsonObject snapshot(Project project, RunnerAndConfigurationSettings settings, String id) {
        List<Map<String, Object>> instances = new ArrayList<>();
        for (RunContentDescriptor descriptor : edt(() -> running(project, settings))) {
            ProcessHandler handler = descriptor.getProcessHandler();
            Long nativePid = handler == null ? null : handler.getNativePid().getNow(null);
            instances.add(Map.of("pid", nativePid == null ? 0 : nativePid, "execution_id", descriptor.getExecutionId()));
        }
        Map<String, Object> values = new LinkedHashMap<>();
        values.put("state", instances.isEmpty() ? "stopped" : "running");
        values.put("scope", "configuration");
        values.put("configuration_verified", true);
        values.put("configuration_name", settings.getName());
        values.put("instances", instances);
        values.put("observed_at", System.currentTimeMillis() / 1000.0);
        Restart operation = OPERATIONS.get(id);
        if (operation != null && operation.key.equals(project.getLocationHash() + ":" + settings.getUniqueID())) values.put("operation", operation.json());
        return object(values);
    }

    private static List<RunContentDescriptor> running(Project project, RunnerAndConfigurationSettings settings) {
        return ExecutionManager.getInstance(project).getRunningDescriptors(candidate -> candidate != null && candidate.getUniqueID().equals(settings.getUniqueID())).stream()
                .filter(descriptor -> descriptor.getProcessHandler() != null && !descriptor.getProcessHandler().isProcessTerminated()).toList();
    }
    private static RunnerAndConfigurationSettings configuration(Project project, String name) {
        List<RunnerAndConfigurationSettings> matches = RunManager.getInstance(project).getAllSettings().stream().filter(settings -> settings.getName().equals(name)).toList();
        if (matches.size() != 1) throw new IllegalArgumentException("项目中未找到唯一同名运行配置");
        return matches.getFirst();
    }
    private static <Value> Value edt(Supplier<Value> task) {
        if (ApplicationManager.getApplication().isDispatchThread()) return task.get();
        AtomicReference<Value> value = new AtomicReference<>();
        ApplicationManager.getApplication().invokeAndWait(() -> value.set(task.get()), ModalityState.any());
        return value.get();
    }
    private static String text(JsonObject values, String field) {
        String value = optional(values, field);
        if (value.isBlank()) throw new IllegalArgumentException("缺少 " + field);
        return value;
    }
    private static String optional(JsonObject values, String field) {
        return values.get(field) instanceof JsonPrimitive primitive ? primitive.getContent() : "";
    }
    private static String safe(Throwable error) {
        if (error instanceof IllegalArgumentException || error instanceof IllegalStateException) return error.getMessage();
        if (error instanceof TimeoutException) return "IDE 运行操作超时，请读取原操作状态";
        return "IDE 运行控制失败：" + error.getClass().getSimpleName();
    }
    private static McpToolCallResult result(JsonObject values, boolean error) {
        return new McpToolCallResult(new McpToolCallResultContent[]{new McpToolCallResultContent.Text(values.toString())}, values, error);
    }
    private static JsonObject object(Map<String, ?> values) {
        Map<String, JsonElement> converted = new LinkedHashMap<>();
        for (Map.Entry<String, ?> entry : values.entrySet()) converted.put(entry.getKey(), json(entry.getValue()));
        return new JsonObject(converted);
    }
    private static JsonElement json(Object value) {
        if (value instanceof JsonElement element) return element;
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> converted = new LinkedHashMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) converted.put(entry.getKey().toString(), entry.getValue());
            return object(converted);
        }
        if (value instanceof Collection<?> collection) return new JsonArray(collection.stream().map(RuntimeTools::json).toList());
        if (value instanceof Boolean booleanValue) return JsonElementKt.JsonPrimitive(booleanValue);
        if (value instanceof Number number) return JsonElementKt.JsonPrimitive(number);
        return JsonElementKt.JsonPrimitive(value.toString());
    }
}
