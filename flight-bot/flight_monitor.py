#!/usr/bin/env python3
"""機票價格監控機器人 — 掃描未來 N 天最便宜航班，低於門檻時發送 Email 通知"""

import os
import csv
import smtplib
import logging
import argparse
from datetime import date, datetime, timedelta
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, field

import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search"


@dataclass
class Route:
    origin: str
    destination: str
    trip_duration_days: int
    threshold: int
    currency: str = "TWD"
    scan_next_days: int = 30
    scan_date_count: int = 8
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"{self.origin}→{self.destination}"

    def candidate_dates(self) -> list[tuple[str, str]]:
        """產生未來 scan_next_days 天內均勻分布的出發/回程日期組合"""
        today = date.today()
        start = today + timedelta(days=3)   # 至少 3 天後才查
        end = today + timedelta(days=self.scan_next_days)
        total = (end - start).days
        step = max(1, total // self.scan_date_count)
        pairs = []
        for i in range(self.scan_date_count):
            depart = start + timedelta(days=i * step)
            ret = depart + timedelta(days=self.trip_duration_days)
            if depart <= end:
                pairs.append((depart.strftime("%Y-%m-%d"), ret.strftime("%Y-%m-%d")))
        return pairs


@dataclass
class FlightOffer:
    route: Route
    price: float
    airline: str
    stops: int
    duration: str
    depart_date: str
    return_date: str
    checked_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


def search_one(route: Route, depart: str, ret: str, api_key: str) -> Optional[FlightOffer]:
    """查詢單一日期組合，回傳最便宜選項"""
    params = {
        "engine": "google_flights",
        "departure_id": route.origin,
        "arrival_id": route.destination,
        "outbound_date": depart,
        "return_date": ret,
        "type": "1",        # round trip
        "currency": route.currency,
        "api_key": api_key,
        "hl": "zh-tw",
    }
    r = requests.get(SERPAPI_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    best_price = None
    best_offer = None
    for section in ("best_flights", "other_flights"):
        for flight in data.get(section, []):
            price = flight.get("price")
            if price is None:
                continue
            if best_price is None or price < best_price:
                best_price = price
                legs = flight.get("flights", [])
                airline = legs[0].get("airline", "未知") if legs else "未知"
                stops = len(legs) - 1 if legs else 0
                mins = flight.get("total_duration", 0)
                h, m = divmod(mins, 60)
                duration = f"{h}h{m:02d}m"
                best_offer = FlightOffer(
                    route=route,
                    price=float(price),
                    airline=airline,
                    stops=stops,
                    duration=duration,
                    depart_date=depart,
                    return_date=ret,
                )
    return best_offer


def find_cheapest(route: Route, api_key: str) -> Optional[FlightOffer]:
    """掃描所有候選日期，回傳最便宜的航班"""
    candidates = route.candidate_dates()
    log.info(f"  掃描 {len(candidates)} 個日期組合...")
    cheapest = None
    for depart, ret in candidates:
        try:
            offer = search_one(route, depart, ret, api_key)
            if offer and (cheapest is None or offer.price < cheapest.price):
                cheapest = offer
                log.info(f"  {depart}→{ret}：{route.currency} {offer.price:,.0f} ({offer.airline})")
        except Exception as e:
            log.warning(f"  {depart} 查詢失敗：{e}")
    return cheapest


class CSVLogger:
    HEADER = ["checked_at", "route", "depart_date", "return_date", "airline",
              "price", "currency", "stops", "duration", "threshold", "alert_sent"]

    def __init__(self, path: str = "price_history.csv"):
        self.path = path
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADER)

    def write(self, offer: FlightOffer, alerted: bool):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                offer.checked_at, offer.route.label,
                offer.depart_date, offer.return_date,
                offer.airline, offer.price, offer.route.currency,
                offer.stops, offer.duration, offer.route.threshold, alerted,
            ])


class Emailer:
    def __init__(self, cfg: dict):
        self.host = cfg["smtp_host"]
        self.port = int(cfg.get("smtp_port", 587))
        self.user = cfg["username"]
        self.pwd = cfg["password"]
        self.sender = cfg["from"]
        self.recipient = cfg["to"]

    def alert(self, offers: list[FlightOffer]):
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"✈️ 機票降價通知！找到 {len(offers)} 筆低於門檻的航班"
        msg["From"] = self.sender
        msg["To"] = self.recipient

        rows = "".join(
            f"<tr>"
            f"<td>{o.route.label}</td>"
            f"<td>{o.depart_date} → {o.return_date}</td>"
            f"<td>{o.airline}</td>"
            f"<td style='color:green'><b>{o.route.currency} {o.price:,.0f}</b></td>"
            f"<td style='color:#888'>門檻: {o.route.threshold:,}</td>"
            f"<td>{'直飛' if o.stops == 0 else f'{o.stops}轉'}</td>"
            f"<td>{o.duration}</td>"
            f"</tr>"
            for o in offers
        )
        html = f"""<html><body style="font-family:sans-serif">
<h2 style="color:#004080">✈️ 找到便宜機票！</h2>
<p style="color:#555">未來 30 天內最低價已低於你設定的門檻</p>
<table border="1" cellpadding="10" cellspacing="0" style="border-collapse:collapse;font-size:14px">
  <tr style="background:#004080;color:white">
    <th>航線</th><th>日期</th><th>航空公司</th><th>價格</th><th>門檻</th><th>停靠</th><th>飛行時間</th>
  </tr>
  {rows}
</table>
<p style="color:#aaa;font-size:12px;margin-top:20px">
  由機票監控機器人自動發送 · {datetime.now().strftime('%Y-%m-%d %H:%M')}
</p>
</body></html>"""

        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(self.host, self.port) as s:
            s.starttls()
            s.login(self.user, self.pwd)
            s.send_message(msg)
        log.info(f"✉️  已發送 Email 通知至 {self.recipient}")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(config_path: str, dry_run: bool = False):
    cfg = load_config(config_path)
    api_key = os.getenv("SERPAPI_KEY") or cfg["serpapi"]["api_key"]
    email_cfg = {k: os.getenv(f"SMTP_{k.upper()}", str(v)) for k, v in cfg["email"].items()}
    mailer = Emailer(email_cfg)
    csv_log = CSVLogger(cfg.get("history_csv", "price_history.csv"))

    global_scan_days = cfg.get("scan_next_days", 30)
    global_date_count = cfg.get("scan_date_count", 8)

    routes = [
        Route(
            origin=r["origin"],
            destination=r["destination"],
            trip_duration_days=r.get("trip_duration_days", 5),
            threshold=r["threshold_price"],
            currency=r.get("currency", "TWD"),
            scan_next_days=r.get("scan_next_days", global_scan_days),
            scan_date_count=r.get("scan_date_count", global_date_count),
        )
        for r in cfg["routes"]
    ]

    log.info(f"開始掃描 {len(routes)} 條航線（未來 {global_scan_days} 天最便宜）...")
    alerts: list[FlightOffer] = []

    for route in routes:
        log.info(f"查詢：{route.label}")
        cheapest = find_cheapest(route, api_key)
        if cheapest is None:
            log.warning("  找不到任何航班")
            continue
        hit = cheapest.price <= route.threshold
        csv_log.write(cheapest, hit)
        status = "🔔 觸發！" if hit else "無變動"
        log.info(
            f"  最低：{route.currency} {cheapest.price:,.0f}  "
            f"({cheapest.depart_date})  門檻：{route.threshold:,}  {status}"
        )
        if hit:
            alerts.append(cheapest)

    if alerts:
        if dry_run:
            log.info(f"[Dry Run] 共 {len(alerts)} 筆低於門檻，略過 Email")
        else:
            mailer.alert(alerts)
    else:
        log.info("沒有航班低於門檻，不發送通知")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="機票價格監控機器人")
    parser.add_argument("--config", default="config.yaml", help="設定檔路徑（預設: config.yaml）")
    parser.add_argument("--dry-run", action="store_true", help="不發送 Email，只顯示結果")
    args = parser.parse_args()
    run(args.config, args.dry_run)
