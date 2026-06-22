import logging
import httpx
from collections import deque
from typing import Any, Dict, List, Tuple

import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.widgets import Button

from shared import config


_VIEW_TITLES: Dict[str, str] = {
    "latency":   "Latency  (RTT)",
    "cpu_usage": "CPU Usage",
    "ram_usage": "RAM Usage",
}

_MI_COLORS: Dict[str, str] = {
    "latency":   "#1565C0",
    "cpu_usage": "#E65100",
    "ram_usage": "#2E7D32",
}

# Échelles fixes par métrique
_Y_LIMITS: Dict[str, tuple] = {
    "latency":   (0, 300),   # ms
    "cpu_usage": (0, 100),   # %
    "ram_usage": (0, 100),   # %
}


class QoSDashboard:

    _C_MEASURED:  str = "#1565C0"
    _C_PREDICT:   str = "#C62828"
    _C_PAST_PRED: str = "#2E7D32"
    _C_THRESHOLD: str = "#E65100"
    _C_VIOLATION: str = "#FFEBEE"
    _C_ACTIVE:    str = "#E8F5E9"

    def __init__(self, logger: logging.Logger) -> None:
        self._logger: logging.Logger = logger
        self._metric_keys: List[str] = list(config.METRICS_REGISTRY.keys())
        self._vm_ids: List[str]       = list(config.VM_REGISTRY.keys())
        self.current_view: int = 0
        self._last_data: Dict[str, Any] = {}

        self._hist_metrics: Dict[str, Dict[str, deque]] = {
            vm: {m: deque(maxlen=100) for m in self._metric_keys}
            for vm in self._vm_ids
        }
        self._hist_cycles: Dict[str, Dict[str, deque]] = {
            vm: {m: deque(maxlen=100) for m in self._metric_keys}
            for vm in self._vm_ids
        }
        self._preds: Dict[str, Dict[str, List[float]]] = {
            vm: {m: [] for m in self._metric_keys}
            for vm in self._vm_ids
        }
        self._hist_preds: Dict[str, Dict[str, deque]] = {
            vm: {m: deque(maxlen=100) for m in self._metric_keys}
            for vm in self._vm_ids
        }
        self._hist_cycles_preds: Dict[str, Dict[str, deque]] = {
            vm: {m: deque(maxlen=100) for m in self._metric_keys}
            for vm in self._vm_ids
        }
        # Historique des poids normalisés des SLOs actifs (somme = 1)
        self._hist_mi: Dict[str, deque] = {
            m: deque(maxlen=100) for m in self._metric_keys
        }
        self._cycles_mi: deque = deque(maxlen=100)

        self._migration_events: deque = deque(maxlen=50)
        self._last_migration_ts: str = ""
        self._paused: bool = False

        plt.ion()
        self._setup_figure()
        self._logger.info("Dashboard initialised", extra={"event": "dashboard_started"})

    def _setup_figure(self) -> None:
        for style in ("seaborn-v0_8-whitegrid", "seaborn-whitegrid", "ggplot"):
            try:
                plt.style.use(style)
                break
            except OSError:
                continue

        self.fig = plt.figure(figsize=(20, 11))
        self.fig.set_facecolor("#F8F9FA")
        try:
            self.fig.canvas.manager.set_window_title("QoS Orchestrator — Real-time Dashboard")
        except AttributeError:
            pass

        gs = GridSpec(2, 4, figure=self.fig,
                      height_ratios=[0.60, 0.40],
                      hspace=0.58, wspace=0.33,
                      top=0.875, bottom=0.07, left=0.055, right=0.945)

        self.ax_metrics: List[plt.Axes] = [
            self.fig.add_subplot(gs[0, i]) for i in range(4)
        ]
        self.ax_mi: plt.Axes = self.fig.add_subplot(gs[1, :])

        ax_prev  = self.fig.add_axes([0.010, 0.895, 0.025, 0.055])
        ax_next  = self.fig.add_axes([0.965, 0.895, 0.025, 0.055])
        ax_pause = self.fig.add_axes([0.455, 0.895, 0.090, 0.055])
        self.btn_prev  = Button(ax_prev,  "◄",        color="#E0E0E0", hovercolor="#BDBDBD")
        self.btn_next  = Button(ax_next,  "►",        color="#E0E0E0", hovercolor="#BDBDBD")
        self.btn_pause = Button(ax_pause, "⏸  Pause", color="#E3F2FD", hovercolor="#BBDEFB")
        self.btn_prev.on_clicked(lambda _e: self.change_view(-1))
        self.btn_next.on_clicked(lambda _e: self.change_view(+1))
        self.btn_pause.on_clicked(lambda _e: self._toggle_pause())

        n = len(self._metric_keys)
        first_title = _VIEW_TITLES.get(self._metric_keys[0], self._metric_keys[0])
        self._view_label = self.fig.text(
            0.50, 0.930, f"Vue 1/{n} — {first_title}",
            ha="center", va="center",
            fontsize=12, fontweight="bold", color="#37474F")

        self.fig.canvas.mpl_connect("key_press_event", self._on_key)

    def fetch_data(self) -> Dict[str, Any]:
        try:
            resp = httpx.get(f"{config.CORE_URL}/data", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                self._logger.info(f"Data fetched — cycle={data.get('cycle', '?')}",
                                  extra={"event": "fetch_success"})
                return data
            self._logger.warning(f"Core returned HTTP {resp.status_code}",
                                 extra={"event": "fetch_failed"})
        except Exception as exc:
            self._logger.warning(f"Core unreachable: {exc}", extra={"event": "fetch_failed"})
        return {}

    def update(self, data: Dict[str, Any]) -> None:
        self._last_data = data
        self._update_histories(data)
        self._draw()

    def _update_histories(self, data: Dict[str, Any]) -> None:
        cycle    = data.get("cycle", 0)
        vms_data = data.get("vms", {})

        for vm in self._vm_ids:
            vm_data = vms_data.get(vm, {})
            for metric, meta in config.METRICS_REGISTRY.items():
                payload_key: str = meta["payload_key"]
                val = vm_data.get(payload_key)

                old_preds = self._preds[vm][metric]
                if old_preds and val is not None:
                    self._hist_preds[vm][metric].append(old_preds[0])
                    self._hist_cycles_preds[vm][metric].append(cycle)

                if val is not None:
                    self._hist_metrics[vm][metric].append(float(val))
                    self._hist_cycles[vm][metric].append(cycle)

                preds = vm_data.get("predictions", {}).get(metric, [])
                self._preds[vm][metric] = [float(p) for p in preds]

        # Poids des SLOs réellement actifs et utilisés par TOPSIS (somme = 1)
        # Remplace les anciens scores MI bruts par les poids normalisés
        # — une métrique non retenue comme SLO ce cycle a un poids de 0.
        slos = data.get("slos", [])
        weights_by_metric: Dict[str, float] = {
            s["metric"]: s.get("weight") for s in slos if "metric" in s
        }

        for metric in self._metric_keys:
            weight = weights_by_metric.get(metric)
            self._hist_mi[metric].append(float(weight) if weight is not None else 0.0)
        self._cycles_mi.append(cycle)

        decision = data.get("last_decision") or {}
        if decision.get("decision") == "migrate":
            ts = decision.get("timestamp", "")
            if ts and ts != self._last_migration_ts:
                self._last_migration_ts = ts
                self._migration_events.append(
                    (cycle, decision.get("from_vm", "?"), decision.get("to_vm", "?"))
                )

    def _draw(self) -> None:
        data = self._last_data

        decision  = data.get("last_decision") or {}
        dec       = decision.get("decision", "none")
        from_vm   = decision.get("from_vm") or "—"
        to_vm     = decision.get("to_vm") or "—"
        score     = decision.get("topsis_score")
        breach    = decision.get("breach_type") or "none"
        cycle     = data.get("cycle", "—")

        active_vm = next(
            (vm for vm, d in data.get("vms", {}).items() if d.get("is_active")), "—")

        is_migrate = dec == "migrate"
        dec_color  = "#B71C1C" if is_migrate else ("#1B5E20" if dec == "stay" else "#546E7A")
        dec_label  = "► MIGRATE" if is_migrate else "► STAY"
        migration  = f" — {from_vm} → {to_vm}" if is_migrate else ""
        score_str  = f"{score:.4f}" if score is not None else "—"

        self.fig.suptitle(
            f"{dec_label}{migration}"
            f"   │   VM active : {active_vm}"
            f"   │   TOPSIS : {score_str}"
            f"   │   Breach : {breach}"
            f"   │   Cycle : {cycle}",
            color=dec_color, fontsize=13, fontweight="bold", y=0.965)

        view_name = self._metric_keys[self.current_view]
        n         = len(self._metric_keys)
        self._view_label.set_text(
            f"Vue {self.current_view + 1}/{n} — {_VIEW_TITLES.get(view_name, view_name)}")

        self._draw_metric_view(view_name)
        self._draw_mi_panel()

        plt.tight_layout(rect=[0.00, 0.00, 1.00, 0.875])
        plt.draw()

        self._logger.info(f"Dashboard updated — view={view_name!r} cycle={cycle}",
                          extra={"event": "gui_update"})

    def _draw_metric_view(self, view_name: str) -> None:
        meta = config.METRICS_REGISTRY[view_name]
        unit = meta["unit"]

        slos = self._last_data.get("slos", [])
        threshold: float = next(
            (float(s["threshold"]) for s in slos if s["metric"] == view_name),
            float(meta["default_threshold"]))

        for idx, vm in enumerate(self._vm_ids):
            ax = self.ax_metrics[idx]
            ax.cla()

            vm_data      = self._last_data.get("vms", {}).get(vm, {})
            is_active    = bool(vm_data.get("is_active", False))
            hist         = list(self._hist_metrics[vm][view_name])
            cycles       = list(self._hist_cycles[vm][view_name])
            hist_pred    = list(self._hist_preds[vm][view_name])
            cycles_pred  = list(self._hist_cycles_preds[vm][view_name])
            preds_future = self._preds[vm][view_name]
            current_val  = hist[-1] if hist else None

            # Background
            in_violation = current_val is not None and current_val > threshold
            if in_violation:
                ax.set_facecolor(self._C_VIOLATION)
            elif is_active:
                ax.set_facecolor(self._C_ACTIVE)
            else:
                ax.set_facecolor("#FFFFFF")

            # ── 1. Courbe réelle (bleue) ──────────────────────────────
            if hist:
                ax.plot(cycles, hist,
                        color=self._C_MEASURED, linewidth=1.8,
                        label=f"Réel: {current_val:.1f} {unit}" if current_val else "Réel",
                        zorder=3)
                ax.plot(cycles[-1], hist[-1], "o",
                        color=self._C_MEASURED, markersize=6, zorder=4)

            # ── 2. Prédictions passées (verte pointillée) ─────────────
            if hist_pred:
                ax.plot(cycles_pred, hist_pred,
                        color=self._C_PAST_PRED, linewidth=1.4,
                        linestyle=":", marker="x", markersize=5,
                        label="Prédit passé", zorder=2, alpha=0.85)

            # ── 3. Prédictions futures (rouge tiretée) ─────────────────
            if preds_future and hist:
                last_cycle = cycles[-1]
                x_p = [last_cycle + i + 1 for i in range(len(preds_future))]
                ax.plot(
                    [last_cycle] + x_p,
                    [hist[-1]] + preds_future,
                    color=self._C_PREDICT, linewidth=1.6,
                    linestyle="--", marker=".",
                    label=f"Prédit futur (×{len(preds_future)})", zorder=3)

            # ── 4. Seuil SLO ──────────────────────────────────────────
            ax.axhline(threshold,
                       color=self._C_THRESHOLD, linewidth=2.0,
                       linestyle="-", alpha=0.9,
                       label=f"SLO {threshold:.0f} {unit}", zorder=2)

            # ── Axes ──────────────────────────────────────────────────
            title_color = "#1B5E20" if is_active else "#263238"
            ax.set_title(vm, fontsize=11, fontweight="bold",
                         color=title_color, pad=4)
            ax.set_xlabel("Cycle", fontsize=8, color="#546E7A")
            ax.set_ylabel(unit, fontsize=9, color="#546E7A")
            ax.tick_params(labelsize=8)
            ax.grid(True, alpha=0.4, linestyle="--", linewidth=0.6)
            ax.legend(fontsize=7.0, loc="upper left",
                      framealpha=0.85, edgecolor="#B0BEC5")

            # ── Y limits FIXES par métrique ───────────────────────────
            if view_name in _Y_LIMITS:
                ax.set_ylim(_Y_LIMITS[view_name])
            else:
                all_vals = list(hist) + list(hist_pred) + preds_future + [threshold]
                if all_vals:
                    ymin = min(all_vals) * 0.85
                    ymax = max(all_vals) * 1.15
                    if ymin == ymax:
                        ymin -= 1
                        ymax += 1
                    ax.set_ylim(ymin, ymax)

            # ── X limits : fenêtre glissante 50 cycles ────────────────
            all_cycles = cycles + cycles_pred
            if all_cycles:
                x_end   = max(all_cycles) + (len(preds_future) if preds_future else 1)
                x_start = max(min(all_cycles), x_end - 50)
                ax.set_xlim(x_start, x_end + 0.5)

            # ── Marqueurs de migration ────────────────────────────────
            y_top = ax.get_ylim()[1]
            for m_cycle, _m_from, m_to in self._migration_events:
                ax.axvline(m_cycle, color="#7B1FA2", linewidth=1.8,
                           linestyle="--", alpha=0.75, zorder=5)
                ax.annotate(
                    f"→{m_to}", xy=(m_cycle, y_top * 0.93),
                    fontsize=7, color="#7B1FA2", ha="center", fontweight="bold",
                    zorder=6)

    def _draw_mi_panel(self) -> None:
        """
        Affiche les poids normalisés des SLOs actifs (utilisés directement
        par TOPSIS pour la décision de migration). La somme des poids
        affichés à un instant donné vaut toujours 1.0 (tant qu'au moins
        un SLO est actif) — une métrique non retenue comme SLO ce cycle
        a un poids de 0.
        """
        ax = self.ax_mi
        ax.cla()

        x_hist   = list(self._cycles_mi)
        has_data = False

        for metric in self._metric_keys:
            scores = list(self._hist_mi[metric])
            if not scores:
                continue
            has_data = True
            n     = len(scores)
            x     = x_hist[-n:] if len(x_hist) >= n else list(range(n))
            color = _MI_COLORS.get(metric, "#607D8B")
            ax.plot(x, scores, color=color, linewidth=2.0, label=metric, zorder=3)
            ax.plot(x[-1], scores[-1], "o", color=color, markersize=5, zorder=4)

        if not has_data:
            ax.text(0.5, 0.5, "Waiting for SLO weight data…",
                    ha="center", va="center", fontsize=11, color="#90A4AE",
                    transform=ax.transAxes)

        ax.set_title("Poids des SLOs actifs — pondération TOPSIS (somme = 1)",
                     fontsize=11, fontweight="bold", color="#263238", pad=6)
        ax.set_xlabel("Cycle", fontsize=9, color="#546E7A")
        ax.set_ylabel("Poids normalisé", fontsize=9, color="#546E7A")
        ax.set_ylim(-0.02, 1.05)
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.4, linestyle="--", linewidth=0.6)
        ax.legend(fontsize=8.5, loc="upper left", framealpha=0.9, edgecolor="#B0BEC5")

    def change_view(self, delta: int) -> None:
        n = len(self._metric_keys)
        self.current_view = (self.current_view + delta) % n
        new_metric = self._metric_keys[self.current_view]
        self._logger.info(f"View changed to '{new_metric}' (index {self.current_view}/{n - 1})",
                          extra={"event": "view_changed"})
        if self._last_data:
            self._draw()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.btn_pause.label.set_text("▶  Resume" if self._paused else "⏸  Pause")
        self.btn_pause.color = "#FFF9C4" if self._paused else "#E3F2FD"
        self._logger.info(
            f"Dashboard {'paused' if self._paused else 'resumed'}",
            extra={"event": "pause_toggled"})
        plt.draw()

    def _on_key(self, event: Any) -> None:
        if event.key == "left":
            self.change_view(-1)
        elif event.key == "right":
            self.change_view(+1)

    def run(self) -> None:
        self._logger.info(
            f"Run loop started — polling {config.CORE_URL}/data every 2 s",
            extra={"event": "dashboard_started"})
        try:
            while plt.fignum_exists(self.fig.number):
                if not self._paused:
                    data = self.fetch_data()
                    if data:
                        self.update(data)
                plt.pause(2.0)
        except Exception as exc:
            self._logger.error(f"Dashboard loop terminated: {exc}",
                               extra={"event": "fetch_failed"})