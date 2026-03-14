#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ArXiv paper tracking engine.

Two-stage funnel: keyword pre-filter → LLM deep evaluation.
Output: Feishu document archived in dedicated folder.
"""

import asyncio
import datetime
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

import yaml

log = logging.getLogger("hub.arxiv")

# Project root for imports
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class ArxivEngine:
    """ArXiv paper tracking engine."""

    def __init__(self, config_path: Path, data_dir: Path):
        self.config_path = Path(config_path)
        self.skill_dir = self.config_path.parent.parent  # .claude/skills/arxiv-tracker/
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "output").mkdir(exist_ok=True)
        self.db_path = self.data_dir / "history.db"
        self._init_db()

    # ── DB ────────────────────────────────────────────────────

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS papers (
                arxiv_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                digest_date TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _dedup_check(self, arxiv_ids: list[str]) -> list[str]:
        """Return arxiv_ids NOT already in the database."""
        if not arxiv_ids:
            return []
        conn = sqlite3.connect(str(self.db_path))
        try:
            cur = conn.cursor()
            existing = set()
            for i in range(0, len(arxiv_ids), 100):
                batch = arxiv_ids[i:i + 100]
                ph = ",".join("?" * len(batch))
                cur.execute(f"SELECT arxiv_id FROM papers WHERE arxiv_id IN ({ph})", batch)
                existing.update(r[0] for r in cur.fetchall())
            return [a for a in arxiv_ids if a not in existing]
        finally:
            conn.close()

    def _dedup_record(self, papers: list[dict], date_str: str):
        """Record processed papers for dedup."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = time.time()
            for p in papers:
                conn.execute(
                    "INSERT OR IGNORE INTO papers (arxiv_id, title, digest_date, created_at) VALUES (?, ?, ?, ?)",
                    (p["arxiv_id"], p.get("title", ""), date_str, now),
                )
            conn.commit()
        finally:
            conn.close()

    # ── Config ────────────────────────────────────────────────

    def _load_config(self) -> dict:
        """Load topics.yaml + merge dynamic keywords if present."""
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

        # Merge dynamic keywords
        dyn_path = self.data_dir / "keywords_dynamic.yaml"
        if dyn_path.exists():
            dyn = yaml.safe_load(dyn_path.read_text(encoding="utf-8")) or {}
            dyn_kw = dyn.get("keywords", {})
            for topic in config.get("topics", []):
                topic_dyn = dyn_kw.get(topic["name"], [])
                active = [k["keyword"] for k in topic_dyn
                          if isinstance(k, dict) and k.get("status") == "active"]
                if active:
                    existing = set(topic["keywords"])
                    topic["keywords"].extend(k for k in active if k not in existing)

        return config

    # ── Stage 0: Fetch ────────────────────────────────────────

    async def _fetch_papers(
        self, target_date: datetime.date, categories: set
    ) -> list[dict]:
        """Fetch papers from arXiv API. Grabs recent 3 days, dedup filters later.

        Uses rate limiting (3s delay, page_size=100) to avoid 429.
        On 429, returns whatever was fetched so far rather than failing.
        """
        import arxiv

        cat_query = " OR ".join(f"cat:{c}" for c in sorted(categories))
        search = arxiv.Search(
            query=cat_query,
            max_results=500,
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )

        cutoff = target_date - datetime.timedelta(days=3)
        papers: list[dict] = []

        def _fetch():
            client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=5)
            try:
                for result in client.results(search):
                    pub_date = result.published.date()
                    if pub_date < cutoff:
                        break
                    papers.append(
                        {
                            "arxiv_id": result.entry_id.split("/abs/")[-1],
                            "title": result.title.replace("\n", " "),
                            "abstract": result.summary.replace("\n", " "),
                            "authors": [a.name for a in result.authors],
                            "categories": list(result.categories),
                            "primary_category": result.primary_category,
                            "published": str(pub_date),
                            "pdf_url": result.pdf_url,
                            "abs_url": result.entry_id,
                        }
                    )
            except Exception as e:
                # On 429 or other errors, return what we have so far
                if papers:
                    log.warning(
                        "arXiv fetch interrupted after %d papers: %s", len(papers), e
                    )
                else:
                    raise

        await asyncio.to_thread(_fetch)
        log.info("Fetched %d papers from arXiv (cutoff %s)", len(papers), cutoff)
        return papers

    # ── Stage 1: Keyword filter ───────────────────────────────

    def _keyword_filter(self, papers: list[dict], topics: list[dict]) -> tuple[list[dict], dict]:
        """Keyword pre-filter on title + abstract. Returns (matched_papers, keyword_hits)."""
        keyword_hits: dict[str, int] = {}
        matched = []

        for paper in papers:
            text = f"{paper['title']} {paper['abstract']}".lower()
            authors_text = " ".join(paper.get("authors", [])).lower()
            hits = []

            for topic in topics:
                # Keyword matching
                for kw in topic.get("keywords", []):
                    if kw.lower() in text:
                        hits.append({"topic": topic["name"], "keyword": kw})
                        keyword_hits[kw] = keyword_hits.get(kw, 0) + 1

                # Author/org matching (for topics with author_orgs)
                for org in topic.get("author_orgs", []):
                    if org.lower() in authors_text or org.lower() in text:
                        hits.append({"topic": topic["name"], "keyword": f"[org:{org}]"})
                        keyword_hits[f"[org:{org}]"] = keyword_hits.get(f"[org:{org}]", 0) + 1

            if hits:
                paper["topic_hits"] = hits
                paper["matched_topics"] = sorted(set(h["topic"] for h in hits))
                matched.append(paper)

        return matched, keyword_hits

    # ── Stage 2: LLM evaluation ──────────────────────────────

    async def _llm_evaluate(self, papers: list[dict], topics: list[dict]) -> list[dict]:
        """LLM deep evaluation via Gemini 3.1 Pro."""
        from agent.llm.gemini_cli import GeminiCli

        config = self._load_config()
        settings = config.get("settings", {})
        batch_size = settings.get("batch_size", 8)
        threshold = settings.get("score_threshold", 3.5)
        model = settings.get("model", "3.1-Pro")

        # Load evaluation prompt template
        prompt_path = self.skill_dir / "prompts" / "evaluate.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")

        # Build topics description (sorted by priority)
        sorted_topics = sorted(topics, key=lambda t: t.get("priority", 99))
        topics_desc = "\n".join(
            f"- **{t['name']}**（优先级{t.get('priority', '-')}）：{t['description']}"
            for t in sorted_topics
        )

        gemini = GeminiCli(config={})
        all_evaluated = []

        for i in range(0, len(papers), batch_size):
            batch = papers[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(papers) + batch_size - 1) // batch_size
            log.info("Evaluating batch %d/%d (%d papers)", batch_num, total_batches, len(batch))

            # Format papers for prompt
            papers_text = ""
            for idx, p in enumerate(batch, 1):
                authors_str = ", ".join(p["authors"][:5])
                if len(p["authors"]) > 5:
                    authors_str += f" et al. ({len(p['authors'])} authors)"
                papers_text += f"\n### 论文 {idx}\n"
                papers_text += f"- arxiv_id: {p['arxiv_id']}\n"
                papers_text += f"- 标题: {p['title']}\n"
                papers_text += f"- 作者: {authors_str}\n"
                papers_text += f"- 类别: {', '.join(p['categories'])}\n"
                papers_text += f"- 匹配话题: {', '.join(p['matched_topics'])}\n"
                papers_text += f"- 摘要: {p['abstract']}\n"

            prompt = prompt_template.replace("{topics_description}", topics_desc)
            prompt = prompt.replace("{papers_batch}", papers_text)

            try:
                result = await gemini.run(
                    prompt=prompt,
                    model=model,
                    timeout_seconds=180,
                )

                if result.is_error:
                    log.error("Gemini evaluation failed for batch %d: %s", batch_num, result.text[:200])
                    continue

                evaluated = self._parse_evaluation(result.text, batch)
                all_evaluated.extend(evaluated)
                log.info("Batch %d: %d papers evaluated", batch_num, len(evaluated))

            except Exception as e:
                log.error("Batch %d evaluation error: %s", batch_num, e)
                continue

        # Filter by threshold and sort
        selected = [p for p in all_evaluated if p.get("overall", 0) >= threshold]
        selected.sort(key=lambda x: x.get("overall", 0), reverse=True)

        log.info("LLM evaluation: %d evaluated → %d selected (threshold %.1f)",
                 len(all_evaluated), len(selected), threshold)
        return selected

    def _parse_evaluation(self, text: str, batch: list[dict]) -> list[dict]:
        """Parse LLM evaluation JSON output, merge scores into paper data."""
        # Extract JSON array (may be wrapped in ```json ... ```)
        json_match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
        if not json_match:
            log.warning("Failed to extract JSON from evaluation output")
            log.debug("Raw output: %s", text[:500])
            return []

        try:
            scores = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            log.warning("Invalid JSON in evaluation: %s", e)
            return []

        # Hallucination check
        if len(scores) != len(batch):
            log.warning("Evaluation count mismatch: expected %d, got %d (possible hallucination)",
                        len(batch), len(scores))

        # Merge scores into paper data
        score_map = {}
        for s in scores:
            aid = s.get("arxiv_id", "")
            if aid:
                score_map[aid] = s

        result = []
        for paper in batch:
            aid = paper["arxiv_id"]
            if aid in score_map:
                s = score_map[aid]
                paper["novelty"] = s.get("novelty", 0)
                paper["rigor"] = s.get("rigor", 0)
                paper["relevance"] = s.get("relevance", 0)
                paper["collaboration"] = s.get("collaboration", 0)
                paper["overall"] = s.get("overall", 0)
                paper["interest_type"] = s.get("interest_type", "business")
                paper["tags"] = s.get("tags", [])
                paper["affiliations"] = s.get("affiliations", "")
                if not paper["affiliations"]:
                    log.warning("Missing affiliations for %s (%s)", aid, paper.get("title", "")[:50])
                paper["reason"] = s.get("reason", "")
                paper["highlight"] = s.get("highlight", "")
                paper["action_hint"] = s.get("action_hint", "")
                result.append(paper)
            else:
                log.debug("No score for %s", aid)

        return result

    # ── Report generation ─────────────────────────────────────

    def _prepare_papers_for_summary(self, papers: list[dict]) -> list[dict]:
        """Prepare paper data for summary prompt (trim abstracts, include new fields)."""
        result = []
        for p in papers:
            result.append({
                "arxiv_id": p["arxiv_id"],
                "title": p["title"],
                "authors": p["authors"][:5],
                "categories": p["categories"],
                "abstract": p["abstract"][:500],
                "overall": p.get("overall", 0),
                "novelty": p.get("novelty", 0),
                "rigor": p.get("rigor", 0),
                "relevance": p.get("relevance", 0),
                "collaboration": p.get("collaboration", 0),
                "interest_type": p.get("interest_type", "business"),
                "tags": p.get("tags", []),
                "affiliations": p.get("affiliations", ""),
                "reason": p.get("reason", ""),
                "highlight": p.get("highlight", ""),
                "action_hint": p.get("action_hint", ""),
                "abs_url": p.get("abs_url", ""),
                "pdf_url": p.get("pdf_url", ""),
            })
        return result

    async def _generate_report(self, papers: list[dict], stats: dict, topics: list[dict]) -> str:
        """Generate business report via Gemini."""
        from agent.llm.gemini_cli import GeminiCli

        business_papers = [p for p in papers if p.get("interest_type") != "personal"]
        if not business_papers:
            return self._simple_report(papers, stats, topics)

        prompt_path = self.skill_dir / "prompts" / "summarize.md"
        prompt_template = prompt_path.read_text(encoding="utf-8")

        config = self._load_config()
        model = config.get("settings", {}).get("model", "3.1-Pro")

        papers_for_summary = self._prepare_papers_for_summary(business_papers)
        personal_count = len([p for p in papers if p.get("interest_type") == "personal"])

        all_cats = set()
        for t in topics:
            all_cats.update(t.get("categories", []))
        cats_str = "/".join(sorted(all_cats))

        prompt = prompt_template
        prompt = prompt.replace("{papers_json}", json.dumps(papers_for_summary, ensure_ascii=False, indent=2))
        prompt = prompt.replace("{categories}", cats_str)
        prompt = prompt.replace("{total_scanned}", str(stats["total_fetched"]))
        prompt = prompt.replace("{keyword_filtered}", str(stats["keyword_matched"]))
        prompt = prompt.replace("{llm_selected}", str(stats["llm_selected"]))
        prompt = prompt.replace("{business_count}", str(len(business_papers)))
        prompt = prompt.replace("{personal_count}", str(personal_count))
        prompt = prompt.replace("{total}", str(stats["total_fetched"]))
        prompt = prompt.replace("{filtered}", str(stats["keyword_matched"]))
        prompt = prompt.replace("{selected}", str(stats["llm_selected"]))

        gemini = GeminiCli(config={})
        try:
            result = await gemini.run(prompt=prompt, model=model, timeout_seconds=120)
            if not result.is_error and result.text.strip():
                return result.text
        except Exception as e:
            log.error("Report generation failed: %s", e)

        # Fallback: simple report
        return self._simple_report(business_papers, stats, topics)

    async def _generate_personal_report(self, papers: list[dict]) -> str:
        """Generate personal interest report via Gemini."""
        from agent.llm.gemini_cli import GeminiCli

        personal_papers = [p for p in papers if p.get("interest_type") == "personal"]
        if not personal_papers:
            return ""

        prompt_path = self.skill_dir / "prompts" / "summarize_personal.md"
        if not prompt_path.exists():
            return self._simple_personal_report(personal_papers)

        prompt_template = prompt_path.read_text(encoding="utf-8")
        config = self._load_config()
        model = config.get("settings", {}).get("model", "3.1-Pro")

        papers_for_summary = self._prepare_papers_for_summary(personal_papers)
        prompt = prompt_template.replace(
            "{papers_json}", json.dumps(papers_for_summary, ensure_ascii=False, indent=2)
        )

        gemini = GeminiCli(config={})
        try:
            result = await gemini.run(prompt=prompt, model=model, timeout_seconds=120)
            if not result.is_error and result.text.strip():
                return result.text
        except Exception as e:
            log.error("Personal report generation failed: %s", e)

        return self._simple_personal_report(personal_papers)

    def _simple_personal_report(self, papers: list[dict]) -> str:
        """Fallback personal interest report without LLM."""
        lines = ["以下论文不直接关联业务方向，但在研究方法、技术趋势等方面有参考价值。\n"]
        for p in sorted(papers, key=lambda x: x.get("overall", 0), reverse=True):
            score = p.get("overall", 0)
            tags_str = " ".join(f"[{t}]" for t in p.get("tags", []))
            lines.append(f"### ⭐ {score:.1f} | {p['title']}")
            if tags_str:
                lines.append(tags_str)
            authors_str = ", ".join(p["authors"][:3])
            if p.get("affiliations"):
                authors_str += f" | {p['affiliations']}"
            lines.append(f"- 作者: {authors_str}")
            if p.get("highlight"):
                lines.append(f"- 亮点: {p['highlight']}")
            if p.get("reason"):
                lines.append(f"- 关注点: {p['reason']}")
            lines.append(f"- [PDF]({p.get('pdf_url', p.get('abs_url', ''))})")
            lines.append("")
        return "\n".join(lines)

    def _simple_report(self, papers: list[dict], stats: dict, topics: list[dict]) -> str:
        """Fallback report without LLM — flat list with tags."""
        all_cats = set()
        for t in topics:
            all_cats.update(t.get("categories", []))

        lines = [
            f"扫描 {'/'.join(sorted(all_cats))} 共 {stats['total_fetched']} 篇，"
            f"预筛 {stats['keyword_matched']} 篇，精选 {stats['llm_selected']} 篇\n"
        ]

        for p in sorted(papers, key=lambda x: x.get("overall", 0), reverse=True):
            score = p.get("overall", 0)
            tags_str = " ".join(f"[{t}]" for t in p.get("tags", []))
            lines.append(f"### ⭐ {score:.1f} | {p['title']}")
            if tags_str:
                lines.append(tags_str)
            authors_str = ", ".join(p["authors"][:3])
            if p.get("affiliations"):
                authors_str += f" | {p['affiliations']}"
            lines.append(f"- 作者: {authors_str}")
            if p.get("highlight"):
                lines.append(f"- 亮点: {p['highlight']}")
            if p.get("reason"):
                lines.append(f"- 理由: {p['reason']}")
            if p.get("action_hint"):
                lines.append(f"- 建议: {p['action_hint']}")
            lines.append(f"- [PDF]({p.get('pdf_url', p.get('abs_url', ''))})")
            lines.append("")

        return "\n".join(lines)

    # ── Feishu publish ────────────────────────────────────────

    def _create_feishu_doc(self, title: str, content_md: str,
                           folder: str = None, share_to: str = None) -> str:
        """Create a Feishu document with given content. Returns doc URL or empty string."""
        import subprocess

        doc_ctl = _REPO_ROOT / ".claude" / "skills" / "feishu-doc" / "scripts" / "doc_ctl.py"

        cmd = [sys.executable, str(doc_ctl), "create", title]
        if folder:
            cmd.extend(["--folder", folder])
        if share_to:
            cmd.extend(["--share", share_to])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    cwd=str(_REPO_ROOT), timeout=60)
        except subprocess.TimeoutExpired:
            log.error("Feishu doc create timed out (60s)")
            return ""
        if result.returncode != 0:
            log.error("Failed to create feishu doc: %s", result.stderr[:300])
            return ""

        doc_match = re.search(r'Created:\s*(\S+)', result.stdout)
        if not doc_match:
            log.error("Cannot parse doc_id from: %s", result.stdout[:200])
            return ""
        doc_id = doc_match.group(1)

        chunks = self._chunk_content(content_md, max_chars=2500)
        for ci, chunk in enumerate(chunks):
            try:
                r = subprocess.run(
                    [sys.executable, str(doc_ctl), "append", doc_id, chunk],
                    capture_output=True, text=True, cwd=str(_REPO_ROOT),
                    timeout=30,
                )
                if r.returncode != 0:
                    # Filter noise from stderr (RequestsDependencyWarning etc.)
                    err = "\n".join(
                        l for l in r.stderr.splitlines()
                        if "Warning" not in l and "warnings.warn" not in l
                    ).strip()
                    if err:
                        log.warning("Append chunk %d failed: %s", ci, err[:200])
            except subprocess.TimeoutExpired:
                log.warning("Append chunk %d timed out", ci)

        doc_url = f"https://feishu.cn/docx/{doc_id}"
        log.info("Published to Feishu: %s", doc_url)
        return doc_url

    async def _publish_to_feishu(self, report_md: str, date_str: str, stats: dict,
                                  personal_report_md: str = "") -> tuple[str, str]:
        """Create Feishu documents. Returns (business_url, personal_url)."""
        config = self._load_config()
        settings = config.get("settings", {})
        topics = config.get("topics", [])

        folder = settings.get("feishu_folder")
        share_to = settings.get("share_to")

        # Business report
        topic_names = "·".join(t["name"] for t in topics)
        title = f"ArXiv 论文日报 — {date_str} | {topic_names}"
        business_url = self._create_feishu_doc(title, report_md, folder, share_to)

        # Personal interest report (separate document)
        personal_url = ""
        if personal_report_md:
            personal_title = f"你可能感兴趣的 — {date_str}"
            personal_url = self._create_feishu_doc(
                personal_title, personal_report_md, folder, share_to
            )

        return business_url, personal_url

    def _chunk_content(self, text: str, max_chars: int = 2500) -> list[str]:
        """Split text into chunks at paragraph boundaries."""
        paragraphs = text.split("\n")
        chunks = []
        current: list[str] = []
        current_len = 0

        for para in paragraphs:
            if current_len + len(para) + 1 > max_chars and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(para)
            current_len += len(para) + 1

        if current:
            chunks.append("\n".join(current))

        return chunks

    # ── Keyword feedback & evolution ──────────────────────────

    def _record_keyword_feedback(self, date_str: str, keyword_hits: dict, total: int):
        """Record keyword hit stats for evolution analysis."""
        feedback_path = self.data_dir / "keyword_feedback.json"
        feedback = {}
        if feedback_path.exists():
            try:
                feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                pass

        feedback[date_str] = {
            "total_papers": total,
            "keyword_hits": keyword_hits,
        }

        # Keep last 30 days
        dates = sorted(feedback.keys())
        if len(dates) > 30:
            for d in dates[:-30]:
                del feedback[d]

        feedback_path.write_text(
            json.dumps(feedback, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    async def _evolve_keywords(self, date_str: str, keyword_hits: dict,
                                missed: list, low_score: list):
        """Analyze keyword effectiveness, save evolution suggestions."""
        from agent.llm.gemini_cli import GeminiCli

        prompt_path = self.skill_dir / "prompts" / "evolve.md"
        if not prompt_path.exists():
            return

        prompt_template = prompt_path.read_text(encoding="utf-8")
        config = self._load_config()

        current_kw = ""
        for t in config.get("topics", []):
            current_kw += f"\n**{t['name']}**: {', '.join(t.get('keywords', []))}\n"

        prompt = prompt_template
        prompt = prompt.replace("{current_keywords}", current_kw)
        prompt = prompt.replace("{keyword_hits}", json.dumps(keyword_hits, ensure_ascii=False))
        prompt = prompt.replace("{missed_samples}",
                                json.dumps(missed[:5] if missed else [], ensure_ascii=False))
        prompt = prompt.replace("{low_score_samples}",
                                json.dumps(low_score[:5] if low_score else [], ensure_ascii=False))

        model = config.get("settings", {}).get("model", "3.1-Pro")
        gemini = GeminiCli(config={})
        try:
            result = await gemini.run(prompt=prompt, model=model, timeout_seconds=90)
            if not result.is_error:
                evo_dir = self.data_dir / "evolution_suggestions"
                evo_dir.mkdir(parents=True, exist_ok=True)
                (evo_dir / f"{date_str}.json").write_text(result.text, encoding="utf-8")
                log.info("Keyword evolution suggestions saved for %s", date_str)
        except Exception as e:
            log.error("Keyword evolution failed: %s", e)

    # ── Trend analysis (dual-model cross-analysis) ──────────

    def _load_trend_state(self) -> dict:
        """Load persistent trend state."""
        path = self.data_dir / "trend_state.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                log.warning("Corrupt trend_state.json, starting fresh")
        return {"last_updated": "", "version": 1, "predictions": []}

    def _save_trend_state(self, state: dict):
        """Save trend state atomically."""
        path = self.data_dir / "trend_state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _prepare_trend_papers(self, papers: list[dict]) -> str:
        """Prepare compact paper summary for trend prompts."""
        lines = []
        for p in papers:
            tags = ", ".join(p.get("tags", []))
            lines.append(
                f"- [{p['arxiv_id']}] {p['title']} | tags: {tags} | "
                f"overall: {p.get('overall', 0)} | "
                f"highlight: {p.get('highlight', '')} | "
                f"action_hint: {p.get('action_hint', '')}"
            )
        return "\n".join(lines) if lines else "(无论文)"

    def _prepare_trend_state_context(self, state: dict) -> str:
        """Prepare trend state for prompt injection (exclude faded)."""
        active = [p for p in state.get("predictions", [])
                  if p.get("trajectory") != "faded"]
        if not active:
            return "(首次运行，无历史趋势)"
        lines = []
        for p in active:
            lines.append(
                f"- [{p['dimension']}] {p['claim']} | "
                f"confidence: {p['confidence']:.2f} | "
                f"trajectory: {p['trajectory']} | "
                f"evidence_count: {p['evidence_count']} | "
                f"first_seen: {p['first_seen']} | "
                f"last_evidence: {p['last_evidence']}"
            )
        return "\n".join(lines)

    async def _trend_round1(self, papers: list[dict], trend_state: dict) -> tuple[str, str]:
        """Round 1: parallel independent analysis by Gemini + Opus."""
        from agent.llm.gemini_cli import GeminiCli
        from agent.llm.claude import ClaudeCli

        prompt_path = self.skill_dir / "prompts" / "trend_round1.md"
        template = prompt_path.read_text(encoding="utf-8")

        papers_summary = self._prepare_trend_papers(papers)
        state_context = self._prepare_trend_state_context(trend_state)

        prompt = template.replace("{papers_summary}", papers_summary)
        prompt = prompt.replace("{trend_state}", state_context)

        config = self._load_config()
        gemini_model = config.get("settings", {}).get("model", "3.1-Pro")

        gemini = GeminiCli(config={})
        claude = ClaudeCli(config={"workspace_dir": str(_REPO_ROOT)})

        # Parallel execution
        gemini_task = gemini.run(prompt=prompt, model=gemini_model, timeout_seconds=300)
        opus_task = claude.run(prompt=prompt, model="claude-opus-4-6", timeout_seconds=300)

        gemini_result, opus_result = await asyncio.gather(
            gemini_task, opus_task, return_exceptions=True
        )

        gemini_text = ""
        opus_text = ""

        if isinstance(gemini_result, Exception):
            log.error("Trend R1 Gemini failed: %s", gemini_result)
        elif gemini_result.is_error:
            log.error("Trend R1 Gemini error: %s", gemini_result.text[:200])
        else:
            gemini_text = gemini_result.text

        if isinstance(opus_result, Exception):
            log.error("Trend R1 Opus failed: %s", opus_result)
        elif opus_result.is_error:
            log.error("Trend R1 Opus error: %s", opus_result.text[:200])
        else:
            opus_text = opus_result.text

        log.info("Trend R1 complete: Gemini=%d chars, Opus=%d chars",
                 len(gemini_text), len(opus_text))
        return gemini_text, opus_text

    async def _trend_round2(self, papers: list[dict], trend_state: dict,
                             r1_gemini: str, r1_opus: str) -> tuple[str, str]:
        """Round 2: parallel cross-calibration."""
        from agent.llm.gemini_cli import GeminiCli
        from agent.llm.claude import ClaudeCli

        prompt_path = self.skill_dir / "prompts" / "trend_round2.md"
        template = prompt_path.read_text(encoding="utf-8")

        papers_summary = self._prepare_trend_papers(papers)
        state_context = self._prepare_trend_state_context(trend_state)

        config = self._load_config()
        gemini_model = config.get("settings", {}).get("model", "3.1-Pro")

        # Gemini sees: my=gemini_r1, peer=opus_r1
        gemini_prompt = template.replace("{papers_summary}", papers_summary)
        gemini_prompt = gemini_prompt.replace("{trend_state}", state_context)
        gemini_prompt = gemini_prompt.replace("{my_analysis}", r1_gemini)
        gemini_prompt = gemini_prompt.replace("{peer_analysis}", r1_opus)

        # Opus sees: my=opus_r1, peer=gemini_r1
        opus_prompt = template.replace("{papers_summary}", papers_summary)
        opus_prompt = opus_prompt.replace("{trend_state}", state_context)
        opus_prompt = opus_prompt.replace("{my_analysis}", r1_opus)
        opus_prompt = opus_prompt.replace("{peer_analysis}", r1_gemini)

        gemini = GeminiCli(config={})
        claude = ClaudeCli(config={"workspace_dir": str(_REPO_ROOT)})

        gemini_task = gemini.run(prompt=gemini_prompt, model=gemini_model, timeout_seconds=300)
        opus_task = claude.run(prompt=opus_prompt, model="claude-opus-4-6", timeout_seconds=300)

        gemini_result, opus_result = await asyncio.gather(
            gemini_task, opus_task, return_exceptions=True
        )

        gemini_text = ""
        opus_text = ""

        if isinstance(gemini_result, Exception):
            log.error("Trend R2 Gemini failed: %s", gemini_result)
        elif gemini_result.is_error:
            log.error("Trend R2 Gemini error: %s", gemini_result.text[:200])
        else:
            gemini_text = gemini_result.text

        if isinstance(opus_result, Exception):
            log.error("Trend R2 Opus failed: %s", opus_result)
        elif opus_result.is_error:
            log.error("Trend R2 Opus error: %s", opus_result.text[:200])
        else:
            opus_text = opus_result.text

        log.info("Trend R2 complete: Gemini=%d chars, Opus=%d chars",
                 len(gemini_text), len(opus_text))
        return gemini_text, opus_text

    async def _trend_render(self, r2_gemini: str, r2_opus: str,
                             trend_state: dict) -> str:
        """Final synthesis by Opus — merge both R2 outputs into report section."""
        from agent.llm.claude import ClaudeCli

        prompt_path = self.skill_dir / "prompts" / "trend_render.md"
        template = prompt_path.read_text(encoding="utf-8")

        state_context = self._prepare_trend_state_context(trend_state)

        prompt = template.replace("{analysis_a}", r2_gemini)
        prompt = prompt.replace("{analysis_b}", r2_opus)
        prompt = prompt.replace("{trend_state}", state_context)

        claude = ClaudeCli(config={"workspace_dir": str(_REPO_ROOT)})
        result = await claude.run(
            prompt=prompt, model="claude-opus-4-6", timeout_seconds=300
        )

        if isinstance(result, Exception) or result.is_error:
            err = result if isinstance(result, Exception) else result.text[:200]
            log.error("Trend render failed: %s", err)
            return ""

        return result.text

    def _update_trend_state(self, trend_state: dict, r2_gemini: str,
                             r2_opus: str, date_str: str) -> dict:
        """Update trend_state based on Round 2 outputs."""
        # Parse both R2 JSONs
        all_claims = []
        all_disagreements = []

        for text in [r2_gemini, r2_opus]:
            json_match = re.search(r'\{.*"dimensions".*\}', text, re.DOTALL)
            if not json_match:
                continue
            try:
                data = json.loads(json_match.group())
            except json.JSONDecodeError:
                continue

            for dim, claims in data.get("dimensions", {}).items():
                for c in claims:
                    c["_dimension"] = dim
                    all_claims.append(c)

            for d in data.get("disagreements", []):
                all_disagreements.append(d)

        # Build existing predictions index
        preds = trend_state.get("predictions", [])
        pred_map = {}
        for p in preds:
            key = f"{p['dimension']}:{p['claim'][:50]}"
            pred_map[key] = p

        # Confidence mapping
        conf_map = {"high": 0.8, "medium": 0.5, "low": 0.3}

        # Process new claims
        seen_keys = set()
        for c in all_claims:
            dim = c.get("_dimension", "")
            claim = c.get("claim", "")
            if not claim:
                continue

            key = f"{dim}:{claim[:50]}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            status = c.get("status", "unchanged")
            conf = conf_map.get(c.get("confidence", "medium"), 0.5)
            evidence = c.get("evidence", [])

            # Find matching existing prediction (fuzzy: same dimension + similar claim start)
            matched_pred = None
            for pk, pv in pred_map.items():
                if pv["dimension"] == dim and (
                    claim[:30].lower() in pv["claim"].lower()
                    or pv["claim"][:30].lower() in claim.lower()
                ):
                    matched_pred = pv
                    break

            if matched_pred:
                # Update existing
                matched_pred["evidence_count"] += 1
                matched_pred["last_evidence"] = date_str
                matched_pred["confidence"] = min(0.95, matched_pred["confidence"] + 0.1)
                if matched_pred["trajectory"] in ("new", "stable"):
                    matched_pred["trajectory"] = "stable"
                if matched_pred["evidence_count"] >= 3:
                    matched_pred["trajectory"] = "strengthening"
                # Update evidence papers
                existing_papers = set(matched_pred.get("evidence_papers", []))
                existing_papers.update(evidence)
                matched_pred["evidence_papers"] = list(existing_papers)[-10:]  # cap
            else:
                # New prediction
                source = "contested" if status == "contested" else "consensus"
                new_conf = conf * 0.5 if status == "contested" else conf
                preds.append({
                    "id": f"trend_{len(preds) + 1:03d}",
                    "claim": claim,
                    "dimension": dim,
                    "confidence": new_conf,
                    "first_seen": date_str,
                    "last_evidence": date_str,
                    "evidence_count": 1,
                    "evidence_papers": evidence[:5],
                    "trajectory": "new",
                    "source": source,
                })

        # Decay: predictions not seen today
        for p in preds:
            if p["last_evidence"] != date_str and p["trajectory"] != "faded":
                days_since = (
                    datetime.date.fromisoformat(date_str)
                    - datetime.date.fromisoformat(p["last_evidence"])
                ).days
                if days_since >= 5:
                    p["confidence"] = max(0.0, p["confidence"] - 0.05 * (days_since - 4))
                    p["trajectory"] = "weakening"
                if p["confidence"] < 0.2:
                    p["trajectory"] = "faded"

        # Cap total predictions
        if len(preds) > 200:
            # Remove oldest faded
            faded = [p for p in preds if p["trajectory"] == "faded"]
            faded.sort(key=lambda x: x.get("last_evidence", ""))
            remove_count = len(preds) - 200
            remove_ids = {p["id"] for p in faded[:remove_count]}
            preds = [p for p in preds if p["id"] not in remove_ids]

        trend_state["predictions"] = preds
        trend_state["last_updated"] = date_str
        return trend_state

    async def _trend_analysis(self, papers: list[dict], stats: dict) -> str:
        """Orchestrate dual-model trend analysis. Returns markdown section."""
        trend_state = self._load_trend_state()
        date_str = stats.get("date", str(datetime.date.today()))

        log.info("Starting trend analysis (%d papers, %d existing predictions)",
                 len(papers), len(trend_state.get("predictions", [])))

        # Round 1: parallel independent analysis
        r1_gemini, r1_opus = await self._trend_round1(papers, trend_state)

        if not r1_gemini and not r1_opus:
            log.error("Trend analysis: both models failed in Round 1")
            return ""

        # Single-model fallback
        if not r1_gemini or not r1_opus:
            single = r1_gemini or r1_opus
            log.warning("Trend analysis: single-model fallback (one model failed in R1)")
            self._update_trend_state(trend_state, single, "", date_str)
            self._save_trend_state(trend_state)
            # Render single analysis (pass as both A and B — prompt merges into all-consensus)
            rendered = await self._trend_render(single, single, trend_state)
            if rendered:
                # Inject degradation notice after the heading
                rendered = rendered.replace(
                    "### 趋势雷达",
                    "### 趋势雷达\n\n> ⚠️ 单模型分析（另一模型超时），置信度降级",
                    1,
                )
                return f"\n\n{rendered}\n"
            # Render failed — still better than raw JSON, use a minimal fallback
            log.warning("Single-model trend render also failed, skipping trend section")
            return ""

        # Round 2: parallel cross-calibration
        r2_gemini, r2_opus = await self._trend_round2(
            papers, trend_state, r1_gemini, r1_opus
        )

        if not r2_gemini and not r2_opus:
            log.error("Trend analysis: both models failed in Round 2, using R1")
            r2_gemini, r2_opus = r1_gemini, r1_opus

        # Update trend state
        self._update_trend_state(trend_state, r2_gemini, r2_opus, date_str)
        self._save_trend_state(trend_state)
        log.info("Trend state updated: %d predictions",
                 len(trend_state.get("predictions", [])))

        # Final render by Opus
        rendered = await self._trend_render(r2_gemini, r2_opus, trend_state)
        if not rendered:
            log.warning("Trend render failed, returning raw R2")
            return f"\n\n---\n\n### 趋势雷达\n\n{r2_gemini}\n\n---\n\n{r2_opus}\n"

        return f"\n\n{rendered}\n"

    # ── Main entry ────────────────────────────────────────────

    async def run(self, date_str: str = None) -> dict:
        """Execute one paper tracking run. Returns status dict."""
        start = time.time()

        # Determine target date
        if date_str:
            target_date = datetime.date.fromisoformat(date_str)
        else:
            target_date = datetime.date.today() - datetime.timedelta(days=1)

        date_str = str(target_date)
        log.info("ArXiv tracker running for %s", date_str)

        config = self._load_config()
        topics = config.get("topics", [])
        if not topics:
            return {"date": date_str, "status": "error", "reason": "no topics configured",
                    "elapsed_s": round(time.time() - start, 1)}

        # Collect all categories
        all_categories = set()
        for t in topics:
            all_categories.update(t.get("categories", []))

        # Stage 0: Fetch
        try:
            papers = await self._fetch_papers(target_date, all_categories)
        except Exception as e:
            log.error("Fetch failed: %s", e)
            return {"date": date_str, "status": "error", "reason": f"fetch failed: {e}",
                    "elapsed_s": round(time.time() - start, 1)}

        total_fetched = len(papers)
        log.info("Fetched %d papers", total_fetched)

        if not papers:
            return {"date": date_str, "status": "skipped", "reason": "no papers found",
                    "total_fetched": 0, "elapsed_s": round(time.time() - start, 1)}

        # Dedup
        new_ids = set(self._dedup_check([p["arxiv_id"] for p in papers]))
        papers = [p for p in papers if p["arxiv_id"] in new_ids]
        log.info("After dedup: %d papers (filtered %d duplicates)",
                 len(papers), total_fetched - len(papers))

        if not papers:
            return {"date": date_str, "status": "skipped", "reason": "all papers already processed",
                    "total_fetched": total_fetched, "elapsed_s": round(time.time() - start, 1)}

        # Stage 1: Keyword filter
        matched, keyword_hits = self._keyword_filter(papers, topics)
        keyword_matched = len(matched)
        log.info("Keyword filter: %d → %d papers", len(papers), keyword_matched)

        # Record feedback regardless
        self._record_keyword_feedback(date_str, keyword_hits, total_fetched)

        if not matched:
            # Record all fetched for dedup even if none matched
            self._dedup_record(papers, date_str)
            return {"date": date_str, "status": "ok", "total_fetched": total_fetched,
                    "keyword_matched": 0, "llm_selected": 0,
                    "elapsed_s": round(time.time() - start, 1)}

        # Stage 2: LLM evaluation
        try:
            selected = await self._llm_evaluate(matched, topics)
        except Exception as e:
            log.error("LLM evaluation failed: %s", e)
            selected = []

        llm_selected = len(selected)
        log.info("LLM evaluation: %d → %d papers", keyword_matched, llm_selected)

        stats = {
            "total_fetched": total_fetched,
            "after_dedup": len(new_ids),
            "keyword_matched": keyword_matched,
            "llm_selected": llm_selected,
        }

        # Record all fetched papers for dedup
        self._dedup_record(papers, date_str)

        if not selected:
            return {"date": date_str, "status": "ok", **stats,
                    "elapsed_s": round(time.time() - start, 1)}

        # Generate reports (business + personal)
        try:
            report = await self._generate_report(selected, stats, topics)
        except Exception as e:
            log.error("Report generation failed: %s", e)
            business_papers = [p for p in selected if p.get("interest_type") != "personal"]
            report = self._simple_report(business_papers or selected, stats, topics)

        personal_report = ""
        try:
            personal_report = await self._generate_personal_report(selected)
        except Exception as e:
            log.error("Personal report generation failed: %s", e)

        # Trend analysis (dual-model cross-analysis)
        trend_section = ""
        try:
            stats["date"] = date_str
            trend_section = await self._trend_analysis(selected, stats)
            if trend_section:
                report += trend_section
                log.info("Trend analysis appended to report")
        except Exception as e:
            log.error("Trend analysis failed (non-blocking): %s", e)

        # Save locally
        output_path = self.data_dir / "output" / f"{date_str}.md"
        output_path.write_text(report, encoding="utf-8")
        if personal_report:
            personal_path = self.data_dir / "output" / f"{date_str}_personal.md"
            personal_path.write_text(personal_report, encoding="utf-8")
        log.info("Report saved to %s", output_path)

        # Publish to Feishu (two documents)
        doc_url = ""
        personal_url = ""
        try:
            doc_url, personal_url = await self._publish_to_feishu(
                report, date_str, stats, personal_report
            )
        except Exception as e:
            log.error("Feishu publish failed: %s", e)

        # Keyword evolution (async, non-blocking on main result)
        low_score = [p for p in matched if p.get("overall", 0) < 3.5 and "overall" in p]
        missed_candidates = [p for p in papers if p not in matched][:10]
        try:
            await self._evolve_keywords(
                date_str, keyword_hits,
                missed=[{"title": p["title"], "abstract": p["abstract"][:200]}
                        for p in missed_candidates[:5]],
                low_score=[{"title": p["title"], "reason": p.get("reason", ""), "overall": p.get("overall", 0)}
                           for p in low_score[:5]],
            )
        except Exception as e:
            log.warning("Keyword evolution skipped: %s", e)

        elapsed = round(time.time() - start, 1)
        result = {
            "date": date_str,
            "status": "ok",
            **stats,
            "doc_url": doc_url,
            "personal_url": personal_url,
            "elapsed_s": elapsed,
        }

        log.info("ArXiv tracker completed: %s", json.dumps(result, ensure_ascii=False))
        return result
