"""Dashboard widgets — each transforms config + cached data into Node trees."""
from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from dashboard.data import Cache, check_service, fetch_grafana_alerts, fetch_json, prom_query
from dashboard.epd import (
    Card, FONT_LG, FONT_MD, FONT_SM, Grid, HeaderBar, Icon, KV, Node,
    ProgressBar, Row, Section, Spacer, StatusDot, Table, Text,
)
from dashboard.system import CpuUsage, cpu_temp, disk_info, mem_info, uptime

if TYPE_CHECKING:
    from dashboard.notifications import Store


# ── Helpers ───────────────────────────────────────────────────────
def _ago(ts: float) -> str:
    d = time.time() - ts
    if d < 60: return "agora"
    if d < 3600: return f"{int(d / 60)}m"
    if d < 86400: return f"{int(d / 3600)}h"
    return f"{int(d / 86400)}d"


def _truncate(text: str, max_len: int = 36) -> str:
    return text if len(text) <= max_len else text[:max_len - 1] + '…'


# ── Base ──────────────────────────────────────────────────────────
class Widget:
    def __init__(self, cfg: dict, cache: Cache) -> None:
        self.cfg = cfg
        self.cache = cache

    def layout(self) -> Node:
        return Spacer(h=0)

    @property
    def refresh_interval(self) -> float:
        return self.cfg.get('interval', 60.0)


# ── Clock ─────────────────────────────────────────────────────────
class ClockWidget(Widget):
    _PT_DAYS = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb', 'Dom']
    _PT_MONTHS = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun',
                  'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

    def layout(self) -> Node:
        now = datetime.now()
        left = f'{Icon.CLOCK} {now.strftime("%H:%M")}'
        date_str = f'{self._PT_DAYS[now.weekday()]} {now.day} {self._PT_MONTHS[now.month - 1]}'
        center = ''
        lat = self.cfg.get('latitude')
        lon = self.cfg.get('longitude')
        if lat is not None and lon is not None:
            data = self.cache.get(
                f'weather:{lat}:{lon}', self.cfg.get('weather_interval', 600),
                lambda: fetch_json(
                    f'https://api.open-meteo.com/v1/forecast?'
                    f'latitude={lat}&longitude={lon}'
                    f'&current=temperature_2m,weather_code'
                    f'&timezone=auto'
                ),
            )
            if data and 'current' in data:
                cur = data['current']
                icon = Icon.wmo(cur.get('weather_code', -1))
                temp = cur.get('temperature_2m', '?')
                center = f'{icon} {temp:.0f}°C' if isinstance(temp, (int, float)) else f'{icon} {temp}°C'
        return HeaderBar(left=left, center=center, right=date_str)


# ── Weather ───────────────────────────────────────────────────────
class WeatherWidget(Widget):
    def layout(self) -> Node:
        lat = self.cfg.get('latitude', 41.54)
        lon = self.cfg.get('longitude', -8.41)
        label = self.cfg.get('label', 'Meteorologia')
        data = self.cache.get(
            f'weather:{lat}:{lon}', self.cfg.get('interval', 600),
            lambda: fetch_json(
                f'https://api.open-meteo.com/v1/forecast?'
                f'latitude={lat}&longitude={lon}'
                f'&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m'
                f'&daily=temperature_2m_max,temperature_2m_min,weather_code'
                f'&timezone=auto&forecast_days=3'
            ),
        )
        if not data or 'current' not in data:
            return Section(label, icon=Icon.SUN, children=[Text('Sem dados', size=FONT_MD)])
        cur = data['current']
        temp = cur.get('temperature_2m', '?')
        humidity = cur.get('relative_humidity_2m', '?')
        wind = cur.get('wind_speed_10m', '?')
        icon = Icon.wmo(cur.get('weather_code', -1))
        children: list[Node] = [
            Row([
                Text(f'{icon} {temp}°C', size=FONT_LG, bold=True),
                Text(f'{Icon.W_HUMID}{humidity}% {Icon.W_WIND}{wind}km/h', size=FONT_MD, align='right'),
            ]),
        ]
        daily = data.get('daily', {})
        highs = daily.get('temperature_2m_max', [])
        lows = daily.get('temperature_2m_min', [])
        codes = daily.get('weather_code', [])
        for i in range(min(3, len(highs))):
            day_label = ['Hoje', 'Amanhã'][i] if i < 2 else daily.get('time', ['', '', ''])[i][5:]
            ic = Icon.wmo(codes[i]) if i < len(codes) else '?'
            children.append(Text(f'{day_label}: {ic} {lows[i]:.0f}-{highs[i]:.0f}°C', size=FONT_MD))
        return Section(label, icon=Icon.SUN, children=children)


# ── System ────────────────────────────────────────────────────────
class SystemWidget(Widget):
    def __init__(self, cfg: dict, cache: Cache) -> None:
        super().__init__(cfg, cache)
        self._cpu = CpuUsage()

    def layout(self) -> Node:
        cpu_pct = self._cpu.read()
        temp = cpu_temp()
        mu, mt, _ = mem_info()
        du, dt, _ = disk_info(self.cfg.get('mount', '/'))
        up = uptime()
        return Section('Sistema', icon=Icon.MONITOR, children=[
            Row([
                Text(f'{Icon.CPU} {cpu_pct} {temp}', size=FONT_SM),
                Text(f'{Icon.RAM} {mu}/{mt}', size=FONT_SM),
            ]),
            Row([
                Text(f'{Icon.DISK} {du}/{dt}', size=FONT_SM),
                Text(f'{Icon.CLOCK} {up}', size=FONT_SM),
            ]),
        ])


# ── Services ──────────────────────────────────────────────────────
class ServicesWidget(Widget):
    def layout(self) -> Node:
        label = self.cfg.get('label', 'Serviços')
        items = self.cfg.get('items', [])
        if not items:
            return Section(label, icon=Icon.SERVER, children=[Text('Sem serviços', size=FONT_SM)])
        cols = self.cfg.get('columns', 3)
        dots: list[Node] = []
        for svc in items:
            name = svc.get('name', '?')
            url = svc.get('url', '')
            status = self.cache.get(
                f'svc:{url}', self.cfg.get('interval', 120),
                lambda u=url: check_service(u),
            )
            dots.append(StatusDot(name, up=status))
        return Section(label, icon=Icon.SERVER,
                       children=[Grid(cols=cols, row_h=14, items=dots)])


# ── Notifications ─────────────────────────────────────────────────
class NotificationsWidget(Widget):
    def __init__(self, cfg: dict, cache: Cache) -> None:
        super().__init__(cfg, cache)
        self.scroll = 0
        self.store: Store | None = None  # set by _build_pages

    def layout(self) -> Node:
        children: list[Node] = []
        if not self.store or not self.store.items:
            children.append(Text('Sem notificações', size=FONT_MD))
        else:
            for n in self.store.items[self.scroll:]:
                marker = Icon.WARNING if n.priority >= 4 else Icon.CHEVRON
                children.append(Row([
                    Text(f'{marker} {n.title}', size=FONT_MD),
                    Text(_ago(n.ts), size=FONT_MD, align='right'),
                ]))
                if n.body:
                    body = n.body if len(n.body) <= 34 else n.body[:32] + '…'
                    children.append(Text(f'  {body}', size=FONT_SM))
        return Section('Notificações', icon=Icon.BELL, children=children)


# ── Media calendar widgets (DRY pattern) ──────────────────────────
class _ApiListWidget(Widget):
    """Base for widgets that fetch a list from an API and render items."""
    _section_label: str = ''
    _section_icon: str = Icon.FILM
    _unconfigured_msg: str = 'Não configurado'

    def _fetch_data(self) -> object | None:
        raise NotImplementedError

    def _render_items(self, data: list) -> list[Node]:
        raise NotImplementedError

    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        if not url or not key:
            return Section(self._section_label, icon=self._section_icon,
                           children=[Text(self._unconfigured_msg, size=FONT_SM)])
        data = self._fetch_data()
        if not data or not isinstance(data, list):
            return Section(self._section_label, icon=self._section_icon,
                           children=[Text('Sem dados', size=FONT_SM)])
        return Section(self._section_label, icon=self._section_icon,
                       children=self._render_items(data))


class NowPlayingWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        if not url or not key:
            return Section('A reproduzir', icon=Icon.PLAY,
                           children=[Text('Tautulli não configurado', size=FONT_SM)])
        data = self.cache.get(
            f'tautulli:{url}', self.cfg.get('interval', 30),
            lambda: fetch_json(f'{url}/api/v2?apikey={key}&cmd=get_activity'),
        )
        sessions = []
        if data and isinstance(data, dict):
            resp = data.get('response', {}).get('data', {})
            sessions = resp.get('sessions', [])
        children: list[Node] = []
        if not sessions:
            children.append(Text('Nada a reproduzir', size=FONT_SM))
        else:
            for s in sessions[:3]:
                title = s.get('title', '?')
                user = s.get('friendly_name', '?')
                state = s.get('state', '?')
                player = s.get('player', '?')
                icon = Icon.PLAY if state == 'playing' else Icon.PAUSE if state == 'paused' else Icon.STOP
                children.append(Text(f'{icon} {title}', size=FONT_SM))
                children.append(Text(f'  {user} • {player}', size=FONT_SM))
        return Section('A reproduzir', icon=Icon.PLAY, children=children)


class SonarrWidget(_ApiListWidget):
    _section_label = 'Séries'
    _unconfigured_msg = 'Sonarr não configurado'

    def _fetch_data(self) -> object | None:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        today = datetime.now().strftime('%Y-%m-%d')
        return self.cache.get(
            f'sonarr:{url}', self.cfg.get('interval', 900),
            lambda: fetch_json(
                f'{url}/api/v3/calendar?start={today}&end=&includeSeries=true&includeEpisodeFile=false',
                headers={'X-Api-Key': key},
            ),
        )

    def _render_items(self, data: list) -> list[Node]:
        children: list[Node] = []
        for ep in data[:4]:
            series = ep.get('series', {}).get('title', '?')
            s = ep.get('seasonNumber', 0)
            e = ep.get('episodeNumber', 0)
            children.append(Text(_truncate(f'• {series} S{s:02d}E{e:02d}'), size=FONT_SM))
        return children


class RadarrWidget(_ApiListWidget):
    _section_label = 'Filmes'
    _unconfigured_msg = 'Radarr não configurado'

    def _fetch_data(self) -> object | None:
        url = self.cfg.get('url', '')
        key = self.cfg.get('api_key', '')
        today = datetime.now().strftime('%Y-%m-%d')
        return self.cache.get(
            f'radarr:{url}', self.cfg.get('interval', 900),
            lambda: fetch_json(
                f'{url}/api/v3/calendar?start={today}',
                headers={'X-Api-Key': key},
            ),
        )

    def _render_items(self, data: list) -> list[Node]:
        children: list[Node] = []
        for movie in data[:4]:
            title = movie.get('title', '?')
            year = movie.get('year', '')
            line = f'• {title} ({year})' if year else f'• {title}'
            children.append(Text(_truncate(line), size=FONT_SM))
        return children


# ── Infrastructure widgets ────────────────────────────────────────
class ProxmoxWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        node = self.cfg.get('node', '')
        user = self.cfg.get('username', '')
        token_name = self.cfg.get('token_name', '')
        token_value = self.cfg.get('token_value', '')
        label = self.cfg.get('label', 'Proxmox')
        if not url or not node:
            return Section(label, children=[Text('Não configurado', size=FONT_SM)])
        headers = {}
        if user and token_name and token_value:
            headers['Authorization'] = f'PVEAPIToken={user}!{token_name}={token_value}'
        data = self.cache.get(
            f'pve:{url}:{node}', self.cfg.get('interval', 120),
            lambda: fetch_json(f'{url}/api2/json/nodes/{node}/status', headers=headers),
        )
        if not data or not isinstance(data, dict):
            return Section(label, children=[Text('Sem dados', size=FONT_SM)])
        d = data.get('data', data)
        cpu = d.get('cpu', 0)
        mem = d.get('memory', {})
        mem_used = mem.get('used', 0)
        mem_total = mem.get('total', 1)
        uptime_s = d.get('uptime', 0)
        mp = 100 * mem_used / mem_total if mem_total else 0
        days = uptime_s // 86400
        hours = (uptime_s % 86400) // 3600
        return Section(label, children=[
            KV(f'{Icon.CPU} CPU', f'{cpu*100:.0f}%'),
            ProgressBar(pct=cpu*100),
            KV(f'{Icon.RAM} RAM', f'{mem_used//(1<<30):.1f}/{mem_total//(1<<30):.1f}G ({mp:.0f}%)'),
            ProgressBar(pct=mp),
            Text(f'{Icon.CLOCK} Up: {days}d {hours}h', size=FONT_SM),
        ])


class HomeAssistantWidget(Widget):
    def layout(self) -> Node:
        url = self.cfg.get('url', '')
        token = self.cfg.get('token', '')
        entities = self.cfg.get('entities', [])
        label = self.cfg.get('label', 'Home')
        if not url or not token or not entities:
            return Section(label, children=[Text('Não configurado', size=FONT_SM)])
        headers_dict = {'Authorization': f'Bearer {token}'}
        children: list[Node] = []
        for eid in entities:
            data = self.cache.get(
                f'ha:{eid}', self.cfg.get('interval', 60),
                lambda e=eid: fetch_json(f'{url}/api/states/{e}', headers=headers_dict),
            )
            if data and isinstance(data, dict):
                name = data.get('attributes', {}).get('friendly_name', eid)
                state = data.get('state', '?')
                unit = data.get('attributes', {}).get('unit_of_measurement', '')
                children.append(Text(f'{name}: {state}{unit}', size=FONT_SM))
            else:
                children.append(Text(f'{eid}: ?', size=FONT_SM))
        return Section(label, children=children)


class ClusterWidget(Widget):
    def _query(self, expr: str) -> list[dict] | None:
        url = self.cfg.get('grafana_url', '')
        if not url:
            return None
        return self.cache.get(
            f'prom:{expr}', self.cfg.get('interval', 120),
            lambda: prom_query(url, expr),
        )

    def layout(self) -> Node:
        grafana_url = self.cfg.get('grafana_url', '')
        badge = ''
        if grafana_url:
            alert_count = self.cache.get(
                f'grafana_alerts:{grafana_url}', 120,
                lambda: fetch_grafana_alerts(grafana_url),
            )
            if alert_count:
                badge = f'{Icon.WARNING} {alert_count}'

        cpu_data = self._query('instance:node_cpu_utilisation:rate5m{job="node-exporter"}')
        ram_data = self._query(
            '1 - (node_memory_MemAvailable_bytes{job="node-exporter"}'
            ' / node_memory_MemTotal_bytes{job="node-exporter"})'
        )
        disk_data = self._query(
            '1 - (node_filesystem_avail_bytes{job="node-exporter",mountpoint="/",fstype!="tmpfs"}'
            ' / node_filesystem_size_bytes{job="node-exporter",mountpoint="/",fstype!="tmpfs"})'
        )

        nodes: dict[str, dict[str, float]] = {}
        for metric, key in [(cpu_data, 'cpu'), (ram_data, 'ram'), (disk_data, 'disk')]:
            if metric:
                for item in metric:
                    inst = item['labels'].get('instance', '?')
                    node_name = item['labels'].get('node', inst.split(':')[0])
                    nodes.setdefault(node_name, {})[key] = (item['value'] or 0) * 100

        if not nodes:
            return Section('Cluster', icon=Icon.SERVER, badge=badge, children=[
                Text('Prometheus indisponível', size=FONT_SM),
            ])

        rows: list[list[str]] = []
        for name in sorted(nodes):
            stats = nodes[name]
            short = name.split('.')[0][:10]
            cpu = f"{stats['cpu']:.0f}%" if 'cpu' in stats else '?'
            ram = f"{stats['ram']:.0f}%" if 'ram' in stats else '?'
            disk_val = f"{stats['disk']:.0f}%" if 'disk' in stats else '?'
            rows.append([short, cpu, ram, disk_val])

        return Section('Cluster', icon=Icon.SERVER, badge=badge, children=[
            Table(
                columns=[('', 0), ('CPU', 76), ('RAM', 136), ('Disco', 196)],
                rows=rows,
            ),
        ])


# ── Toast builder ─────────────────────────────────────────────────
def build_toasts(store) -> list[Card]:
    """Build toast Card nodes from notifications."""
    toasts: list[Card] = []
    for n in store.items[:3]:
        marker = Icon.WARNING if n.priority >= 4 else Icon.BELL
        children: list[Node] = [
            Row([
                Text(f'{marker} {n.title}', size=FONT_MD, bold=True),
                Text(_ago(n.ts), size=FONT_SM, align='right'),
            ]),
        ]
        if n.body:
            body = n.body if len(n.body) <= 32 else n.body[:30] + '…'
            children.append(Text(f'  {body}', size=FONT_SM))
        toasts.append(Card(children=children))
    return toasts


# ── Registry ──────────────────────────────────────────────────────
WIDGET_TYPES: dict[str, type[Widget]] = {
    "clock": ClockWidget,
    "weather": WeatherWidget,
    "system": SystemWidget,
    "services": ServicesWidget,
    "notifications": NotificationsWidget,
    "now_playing": NowPlayingWidget,
    "sonarr": SonarrWidget,
    "radarr": RadarrWidget,
    "proxmox": ProxmoxWidget,
    "home_assistant": HomeAssistantWidget,
    "cluster": ClusterWidget,
}
