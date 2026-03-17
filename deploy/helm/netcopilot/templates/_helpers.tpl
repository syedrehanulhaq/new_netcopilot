{{- define "netcopilot.name" -}}
netcopilot
{{- end -}}

{{- define "netcopilot.fullname" -}}
{{- printf "%s" (include "netcopilot.name" .) -}}
{{- end -}}
