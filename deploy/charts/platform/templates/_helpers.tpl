{{- define "platform.labels" -}}
app.kubernetes.io/part-of: urumi-platform
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "platform.host" -}}
{{ index .root.Values.hosts .name }}.{{ .root.Values.global.domain }}
{{- end -}}

{{- define "platform.selector" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{/* Hardened container defaults for our own images (non-root, read-only root fs, no capabilities). */}}
{{- define "platform.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: { drop: ["ALL"] }
{{- end -}}

{{- define "platform.podSecurity" -}}
runAsNonRoot: true
runAsUser: 10001
seccompProfile: { type: RuntimeDefault }
{{- end -}}

{{/* Host headers the in-cluster MCP services accept (DNS-rebinding protection). */}}
{{- define "platform.serviceHosts" -}}
{{ .name }}:*,{{ .name }}.{{ .root.Release.Namespace }}:*,{{ .name }}.{{ .root.Release.Namespace }}.svc:*,{{ .name }}.{{ .root.Release.Namespace }}.svc.cluster.local:*,localhost:*,127.0.0.1:*
{{- end -}}
