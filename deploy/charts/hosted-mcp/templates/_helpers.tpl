{{- define "hosted.labels" -}}
app.kubernetes.io/name: mcp
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: urumi-hosted-mcp
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
urumi.io/mcp-upstream: "true"
{{- end -}}

{{- define "hosted.selectorLabels" -}}
app.kubernetes.io/name: mcp
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
