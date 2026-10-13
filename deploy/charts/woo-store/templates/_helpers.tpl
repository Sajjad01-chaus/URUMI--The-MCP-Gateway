{{- define "store.host" -}}
{{ .Values.store.subdomain }}.{{ .Values.global.domain }}
{{- end -}}

{{- define "store.url" -}}
{{ if .Values.global.tls.enabled }}https{{ else }}http{{ end }}://{{ include "store.host" . }}
{{- end -}}

{{- define "store.labels" -}}
app.kubernetes.io/part-of: woo-store
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{/* Env shared by WordPress and the bootstrap Job: wp-config.php reads these at runtime. */}}
{{- define "store.wordpressEnv" -}}
- name: WORDPRESS_DB_HOST
  value: mysql:3306
- name: WORDPRESS_DB_NAME
  value: {{ .Values.mysql.database | quote }}
- name: WORDPRESS_DB_USER
  value: {{ .Values.mysql.user | quote }}
- name: WORDPRESS_DB_PASSWORD
  valueFrom:
    secretKeyRef: { name: mysql-auth, key: password }
- name: WORDPRESS_CONFIG_EXTRA
  value: |
    define('WP_HOME', '{{ include "store.url" . }}');
    define('WP_SITEURL', '{{ include "store.url" . }}');
    if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') { $_SERVER['HTTPS'] = 'on'; }
{{- end -}}
