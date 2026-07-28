from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .reconcile import _tenths


SCHEMA = """
CREATE TABLE IF NOT EXISTS book_events (
  id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, ticker TEXT,
  event_type TEXT NOT NULL, seq INTEGER, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
  id INTEGER PRIMARY KEY, station_id TEXT NOT NULL, observed_at TEXT NOT NULL,
  temperature_c REAL NOT NULL,
  temperature_f_round_cf REAL NOT NULL,
  temperature_f_round_c REAL NOT NULL,
  temperature_f_round_cff REAL NOT NULL,
  running_extreme_cf REAL NOT NULL,
  running_extreme_c REAL NOT NULL,
  running_extreme_cff REAL NOT NULL,
  selected_temperature_f REAL NOT NULL,
  selected_running_extreme_f REAL NOT NULL,
  observation_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, ticker TEXT NOT NULL,
  kind TEXT NOT NULL, side TEXT NOT NULL, executable_price_cents INTEGER NOT NULL,
  gross_gap_cents INTEGER NOT NULL, required_gap_cents REAL NOT NULL,
  displayed_size INTEGER NOT NULL, quote_age_seconds REAL,
  would_have_filled INTEGER NOT NULL, payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY, station_id TEXT NOT NULL, date TEXT NOT NULL,
  parsed_cf_tenths INTEGER NOT NULL,
  parsed_c_tenths INTEGER NOT NULL,
  parsed_cff_tenths INTEGER NOT NULL,
  selected_parsed_tenths INTEGER NOT NULL,
  settled_tenths INTEGER NOT NULL,
  signal_fired INTEGER NOT NULL, would_have_filled INTEGER NOT NULL,
  agreed_cf INTEGER NOT NULL,
  agreed_c INTEGER NOT NULL,
  agreed_cff INTEGER NOT NULL,
  selected_agreed INTEGER NOT NULL,
  UNIQUE(station_id, date)
);
"""


class ResearchStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _json(value: Any) -> str:
        if is_dataclass(value):
            value = asdict(value)
        return json.dumps(value, default=str, sort_keys=True)

    def add_book_event(self, captured_at: str, event_type: str, payload: dict[str, Any]) -> None:
        body = payload.get("msg", payload.get("data", {}))
        self.conn.execute(
            "INSERT INTO book_events(captured_at,ticker,event_type,seq,payload_json) VALUES(?,?,?,?,?)",
            (captured_at, body.get("market_ticker") or body.get("ticker"), event_type, payload.get("seq"), self._json(payload)),
        )
        self.conn.commit()

    def add_observation(
        self,
        *,
        station_id: str,
        observed_at: str,
        temperature_c: float,
        temperature_f_round_cf: float,
        temperature_f_round_c: float,
        temperature_f_round_cff: float,
        running_extreme_cf: float,
        running_extreme_c: float,
        running_extreme_cff: float,
        selected_temperature_f: float,
        selected_running_extreme_f: float,
        observation_type: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO observations(
               station_id,observed_at,temperature_c,temperature_f_round_cf,temperature_f_round_c,
               temperature_f_round_cff,running_extreme_cf,running_extreme_c,running_extreme_cff,
               selected_temperature_f,selected_running_extreme_f,observation_type
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                station_id,
                observed_at,
                temperature_c,
                temperature_f_round_cf,
                temperature_f_round_c,
                temperature_f_round_cff,
                running_extreme_cf,
                running_extreme_c,
                running_extreme_cff,
                selected_temperature_f,
                selected_running_extreme_f,
                observation_type,
            ),
        )
        self.conn.commit()

    def add_signal(self, captured_at: str, signal: Any, required_gap_cents: float, quote_age_seconds: float | None, would_have_filled: bool) -> None:
        self.conn.execute(
            """INSERT INTO signals(captured_at,ticker,kind,side,executable_price_cents,gross_gap_cents,
               required_gap_cents,displayed_size,quote_age_seconds,would_have_filled,payload_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (captured_at, signal.ticker, signal.kind, signal.side, signal.executable_price_cents,
             signal.gross_gap_cents, required_gap_cents, signal.displayed_size, quote_age_seconds,
             int(would_have_filled), self._json(signal)),
        )
        self.conn.commit()

    def add_reconciliation(
        self,
        *,
        station_id: str,
        date: str,
        parsed_cf_value: float,
        parsed_c_value: float,
        parsed_cff_value: float,
        selected_parsed_value: float,
        settled_value: float,
        signal_fired: bool,
        would_have_filled: bool,
        tolerance_tenths: int = 0,
    ) -> None:
        parsed_cf = _tenths(parsed_cf_value)
        parsed_c = _tenths(parsed_c_value)
        parsed_cff = _tenths(parsed_cff_value)
        selected = _tenths(selected_parsed_value)
        settled = _tenths(settled_value)
        agreed_cf = abs(parsed_cf - settled) <= tolerance_tenths
        agreed_c = abs(parsed_c - settled) <= tolerance_tenths
        agreed_cff = abs(parsed_cff - settled) <= tolerance_tenths
        selected_agreed = abs(selected - settled) <= tolerance_tenths
        self.conn.execute(
            """INSERT INTO reconciliations(
               station_id,date,parsed_cf_tenths,parsed_c_tenths,parsed_cff_tenths,
               selected_parsed_tenths,settled_tenths,signal_fired,would_have_filled,
               agreed_cf,agreed_c,agreed_cff,selected_agreed
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(station_id,date) DO UPDATE SET
               parsed_cf_tenths=excluded.parsed_cf_tenths,
               parsed_c_tenths=excluded.parsed_c_tenths,
               parsed_cff_tenths=excluded.parsed_cff_tenths,
               selected_parsed_tenths=excluded.selected_parsed_tenths,
               settled_tenths=excluded.settled_tenths,
               signal_fired=excluded.signal_fired,
               would_have_filled=excluded.would_have_filled,
               agreed_cf=excluded.agreed_cf,
               agreed_c=excluded.agreed_c,
               agreed_cff=excluded.agreed_cff,
               selected_agreed=excluded.selected_agreed""",
            (
                station_id,
                date,
                parsed_cf,
                parsed_c,
                parsed_cff,
                selected,
                settled,
                int(signal_fired),
                int(would_have_filled),
                int(agreed_cf),
                int(agreed_c),
                int(agreed_cff),
                int(selected_agreed),
            ),
        )
        self.conn.commit()

    def reconciliation_counts(
        self,
        *,
        signal_only: bool = False,
        fill_only: bool = False,
        candidate: str = "selected",
    ) -> tuple[int, int]:
        if signal_only and fill_only:
            raise ValueError("choose signal_only or fill_only, not both")
        agreed_column = {
            "selected": "selected_agreed",
            "cf": "agreed_cf",
            "c": "agreed_c",
            "cff": "agreed_cff",
        }.get(candidate)
        if agreed_column is None:
            raise ValueError("candidate must be selected, cf, c, or cff")
        where = "WHERE signal_fired=1" if signal_only else ("WHERE would_have_filled=1" if fill_only else "")
        total, errors = self.conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {agreed_column}=0 THEN 1 ELSE 0 END),0) FROM reconciliations {where}"
        ).fetchone()
        return int(total), int(errors)
