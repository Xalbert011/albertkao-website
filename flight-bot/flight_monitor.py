#!/usr/bin/env python3
"""機票價格監控機器人 — 使用 SerpAPI (Google Flights) 查詢票價，低於門檻時發送 Email 通知"""

import os
import csv
import smtplib
import logging
import argparse
from datetime import datetime
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
    depart_date: str
    return_date: Optional[str]
    threshold: int
    currency: str = "TWD"
    label: str = ""

    def __post_init__(self):
        if not self.label:
            self.label = f"{self.origin}→{self.destination} ({self.depart_date})"


@dataclass
class FlightOffer:
    route: Route
    price: float
    airline: str
    stops: int
    duration: str
    checked_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


def search_flights(route: Route, api_key: str) -> list[FlightOffer]:
    """使用 SerpAPI 查詢 Google Flights 價格"""
    params = {
        "engine": "google_flights",
        "departure_id": route.origin,
        "arrival_id": route.destination,
        "outbound_date": route.depart_date,
        "currency": route.currency,
        "api_key": api_key,
        "hl": "zh-tw",
    }
    if route.return_date:
        params["return_date"] = route.return_date
        params["type"] = "1"   # round trip
    else:
        params["type"] = "2"   # one way

    r = requests.get(SERPAPI_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    offers: list[FlightOffer] = []
    for section in ("best_flights", "other_flights"):
        for flight in data.get(section, []):
            price = flight.get("price")
            if price is None:
                continue
            legs = flight.get("flights", [])
            airline = legs[0].get("airline", "未知") if legs else "未知"
            stops = len(legs) - 1 if legs else 0
            duration_min = flight.get("total_duration", 0)
            h, m = divmod(duration_min, 60)
            duration = f"{h}小時{m}分" if m else f"{h}小時"
            offers.append(FlightOffer(
                route=route,
                price=float(price),
                airline=airline,
                stops=stops,
                duration=duration,
            ))

    return sorted(offers, key=lambda x: x.price)


class CSVLogger:
    HEADER = ["checked_at", "route", "airline", "price", "currency",
              "stops", "duration", "threshold", "alert_sent"]

    def __init__(self, path: str = "price_history.csv"):
        self.path = path
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self.HEADER)

    def write(self, offer: FlightOffer, alerted: bool):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                offer.checked_at, offer.route.label, offer.airline,
                offer.price, offer.route.currency, offer.stops,
                offer.duration, offer.route.threshold, alerted,
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
            f"<td>{o.airline}</td>"
            f"<td style='color:green'><b>{o.route.currency} {o.price:,.0f}</b></td>"
            f"<td style='color:#888'>門檻: {o.route.threshold:,}</td>"
            f"<td>{'直飛' if o.stops == 0 else f'{o.stops}次轉機'}</td>"
            f"<td>{o.duration}</td>"
            f"</tr>"
            for o in offers
        )
        html = f"""<html><body style="font-family:sans-serif">
<h2 style="color:#004080">✈️ 找到便宜機票！</h2>
<table border="1" cellpadding="10" cellspacing="0" style="border-collapse:collapse;font-size:14px">
  <tr style="background:#004080;color:white">
    <th>航線</th><th>航空公司</th><th>價格</th><th>門檻</th><th>停靠</th><th>飛行時間</th>
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
        log.info(f"✉️  已發送 Email 通知，共 {len(offers)} 筆")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run(config_path: str, dry_run: bool = False):
    cfg = load_config(config_path)

    api_key = os.getenv("SERPAPI_KEY") or cfg["serpapi"]["api_key"]
    email_cfg = {k: os.getenv(f"SMTP_{k.upper()}", v) for k, v in cfg["email"].items()}
    mailer = Emailer(email_cfg)
    csv_log = CSVLogger(cfg.get("history_csv", "price_history.csv"))

    routes = [
        Route(
            origin=r["origin"],
            destination=r["destination"],
            depart_date=r["depart_date"],
            return_date=r.get("return_date"),
            threshold=r["threshold_price"],
            currency=r.get("currency", "TWD"),
        )
        for r in cfg["routes"]
    ]

    log.info(f"開始監控 {len(routes)} 條航線...")
    alerts: list[FlightOffer] = []

    for route in routes:
        log.info(f"查詢: {route.label}")
        try:
            offers = search_flights(route, api_key)
            if not offers:
                log.warning("  找不到任何航班")
                continue
            best = offers[0]
            hit = best.price <= route.threshold
            csv_log.write(best, hit)
            status = "🔔 觸發！" if hit else "無變動"
            log.info(f"  最低價: {route.currency} {best.price:,.0f}  門檻: {route.threshold:,}  {status}")
            if hit:
                alerts.append(best)
        except Exception as e:
            log.error(f"  查詢失敗: {e}")

    if alerts:
        if dry_run:
            log.info(f"[Dry Run] 共 {len(alerts)} 筆低於門檻，略過 Email 發送")
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
