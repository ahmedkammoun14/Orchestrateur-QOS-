# Clés Redis centralisées — jamais hardcodées ailleurs

METRICS_KEY:   str = "metrics:{vm_id}:{metric}"
HISTORY_KEY:   str = "metrics:{vm_id}:history"
SLOS_KEY:      str = "slos:active"
DECISIONS_KEY: str = "decisions:recent"