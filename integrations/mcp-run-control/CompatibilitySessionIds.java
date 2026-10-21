package dev.lewik.jetbrainsmcp.runcontrol;

import com.intellij.openapi.project.Project;
import com.intellij.execution.ui.RunContentDescriptor;

/** Local binary compatibility bridge for PyCharm 261's native MCP API. */
final class CompatibilitySessionIds {
    private CompatibilitySessionIds() {}

    public static String buildSessionId(Project project, String executorId,
                                        String sessionName, RunContentDescriptor descriptor) {
        return project.getLocationHash() + ":" + executorId + ":" + sessionName + ":"
            + descriptor.getExecutionId() + ":"
            + Integer.toUnsignedString(System.identityHashCode(descriptor), 36);
    }
}
