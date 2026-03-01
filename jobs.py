# -*- coding: utf-8 -*-
"""
Background job handling for the Gemini Book Summarizer plugin.
Handles book text extraction, API calls, and saving results.
"""

import os
import traceback
import json
import subprocess
import time
import re
import socket
from urllib import request as urlrequest
from urllib import error as urlerror

try:
    from qt.core import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                          QPushButton, QProgressBar, QTextEdit,
                          QThread, pyqtSignal)
except ImportError:
    from PyQt5.Qt import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                           QPushButton, QProgressBar, QTextEdit,
                           QThread, pyqtSignal)


# ─────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────

class RetryableGeminiError(RuntimeError):
    def __init__(self, message, retry_after_seconds=None):
        RuntimeError.__init__(self, message)
        self.retry_after_seconds = retry_after_seconds


class SummarizerWorker(QThread):
    """Worker thread that calls Gemini API for each book."""
    MAX_GEMINI_RETRIES = 3
    DEFAULT_RETRY_DELAY_SECONDS = 5.0
    REQUEST_TIMEOUT_SECONDS = 180
    DEFAULT_MAX_BOOK_WORDS = 500_000
    EXTRACTION_CHAR_BUDGET = 2_000_000
    RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}

    progress   = pyqtSignal(int, str)   # (current_index, message)
    book_done  = pyqtSignal(int, str)   # (book_id, summary_text)
    book_error = pyqtSignal(int, str)   # (book_id, error_message)
    finished   = pyqtSignal()

    def __init__(self, db, book_ids, api_key, model, prompt_template, max_words, max_input_words):
        QThread.__init__(self)
        self.db              = db
        self.book_ids        = book_ids
        self.api_key         = api_key
        self.model           = model
        self.prompt_template = prompt_template
        self.max_words       = max_words
        self.max_input_words = int(max_input_words or self.DEFAULT_MAX_BOOK_WORDS)
        self._cancelled      = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            total = len(self.book_ids)
            for idx, book_id in enumerate(self.book_ids):
                if self._cancelled:
                    break

                try:
                    mi = self.db.get_metadata(book_id)
                    title   = mi.title or 'Unknown Title'
                    authors = ', '.join(mi.authors) if mi.authors else 'Unknown Author'

                    self.progress.emit(idx, '')
                    self.progress.emit(idx, f'[{idx+1}/{total}] {title}')
                    self.progress.emit(idx, '  Stage: Extracting text')
                    content, details = self._extract_book_text(
                        book_id,
                        title,
                        max_words=self.max_input_words,
                        char_budget=self.EXTRACTION_CHAR_BUDGET,
                    )
                    available_formats = details.get('formats') or []
                    self.progress.emit(idx, f'    - Available formats: {", ".join(available_formats) if available_formats else "none"}')
                    self.progress.emit(idx, f'    - Chosen format: {details.get("chosen_fmt") or "unknown"}')
                    if details.get('path'):
                        self.progress.emit(idx, f'    - Source path: {details["path"]}')
                    if details.get('extractor'):
                        self.progress.emit(idx, f'    - Extractor: {details["extractor"]}')

                    if not content:
                        if details.get('error'):
                            self.progress.emit(idx, f'    - Extraction detail: {details["error"]}')
                        self.book_error.emit(book_id, 'Could not extract text from book (no supported format found).')
                        continue
                    self.progress.emit(
                        idx,
                        f'    - Extracted text: {details.get("word_count", 0)} words, {len(content)} chars'
                    )
                    if details.get('truncated'):
                        self.progress.emit(
                            idx,
                            f'    - Extraction was truncated at {details.get("max_words", self.max_input_words)} words'
                        )

                    self.progress.emit(idx, '  Stage: Calling Gemini API')
                    prompt = self.prompt_template.format(
                        title=title,
                        authors=authors,
                        text=content,
                        max_words=self.max_words
                    )
                    self.progress.emit(idx, f'    - Model: {self.model}')
                    self.progress.emit(
                        idx,
                        f'    - Prompt size: {len(prompt.split())} words, {len(prompt)} chars'
                    )

                    summary, api_meta = self._call_gemini_with_retries(prompt, idx)
                    self.progress.emit(idx, f'    - API candidates: {api_meta.get("candidates", 0)}')
                    if api_meta.get('finish_reason'):
                        self.progress.emit(idx, f'    - Finish reason: {api_meta["finish_reason"]}')
                    self.progress.emit(idx, f'    - Summary characters: {len(summary)}')
                    if not summary:
                        raise ValueError('Gemini returned an empty response.')
                    self.book_done.emit(book_id, summary)

                except Exception as e:
                    self.book_error.emit(book_id, traceback.format_exc())

        except Exception as e:
            self.book_error.emit(-1, f'Fatal error: {traceback.format_exc()}')
        finally:
            self.finished.emit()

    # ─── helpers ─────────────────────────────

    def _call_gemini_with_retries(self, prompt, idx):
        total_attempts = self.MAX_GEMINI_RETRIES + 1
        attempt = 1
        while True:
            try:
                if attempt > 1:
                    self.progress.emit(idx, f'    - Retry attempt: {attempt}/{total_attempts}')
                return self._call_gemini(prompt)
            except RetryableGeminiError as e:
                if attempt > self.MAX_GEMINI_RETRIES:
                    raise RuntimeError(
                        f'Gemini request still failing after {self.MAX_GEMINI_RETRIES} retries: {e}'
                    )

                wait_seconds = e.retry_after_seconds
                if wait_seconds is None:
                    wait_seconds = self.DEFAULT_RETRY_DELAY_SECONDS * attempt
                wait_seconds = max(1.0, float(wait_seconds))
                self.progress.emit(
                    idx,
                    f'    - Retryable error: {e}. Waiting {wait_seconds:.1f}s before retry {attempt + 1}/{total_attempts}.'
                )
                if not self._sleep_with_cancel(wait_seconds):
                    raise RuntimeError('Cancelled while waiting to retry Gemini request.')

                attempt += 1

    def _sleep_with_cancel(self, seconds):
        end = time.time() + max(0.0, float(seconds))
        while time.time() < end:
            if self._cancelled:
                return False
            remaining = end - time.time()
            time.sleep(min(0.5, max(0.0, remaining)))
        return not self._cancelled

    def _parse_retry_delay_seconds(self, error_payload):
        details = (error_payload or {}).get('details') or []
        for detail in details:
            retry_delay = (detail or {}).get('retryDelay')
            if not retry_delay:
                continue
            match = re.match(r'^\s*(\d+(?:\.\d+)?)s\s*$', str(retry_delay))
            if match:
                try:
                    return float(match.group(1))
                except Exception:
                    return None
        return None

    def _parse_retry_after_header_seconds(self, headers):
        if not headers:
            return None
        retry_after = headers.get('Retry-After')
        if not retry_after:
            return None
        retry_after = str(retry_after).strip()
        if retry_after.isdigit():
            try:
                return float(retry_after)
            except Exception:
                return None
        return None

    def _call_gemini(self, prompt):
        """Call Gemini REST API without external SDK dependencies."""
        safe_endpoint = (
            'https://generativelanguage.googleapis.com/v1beta/models/'
            f'{self.model}:generateContent'
        )
        endpoint = (
            'https://generativelanguage.googleapis.com/v1beta/models/'
            f'{self.model}:generateContent?key={self.api_key}'
        )
        payload = {
            'contents': [{'parts': [{'text': prompt}]}]
        }
        data = json.dumps(payload).encode('utf-8')
        req = urlrequest.Request(
            endpoint,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with urlrequest.urlopen(req, timeout=self.REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read().decode('utf-8', errors='replace')
        except urlerror.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace')
            if e.code in self.RETRYABLE_HTTP_CODES:
                retry_after = self._parse_retry_after_header_seconds(getattr(e, 'headers', None))
                parsed = None
                try:
                    parsed = json.loads(body)
                except Exception:
                    parsed = None
                if retry_after is None and parsed:
                    retry_after = self._parse_retry_delay_seconds(parsed.get('error') or {})
                raise RetryableGeminiError(
                    f'Gemini HTTP {e.code}',
                    retry_after_seconds=retry_after,
                )
            raise RuntimeError(f'Gemini HTTP {e.code} on {safe_endpoint}: {body}')
        except (TimeoutError, socket.timeout) as e:
            raise RetryableGeminiError(
                f'Gemini request timed out after {self.REQUEST_TIMEOUT_SECONDS}s: {e}'
            )
        except urlerror.URLError as e:
            reason = str(getattr(e, 'reason', e))
            timeout_like = 'timed out' in reason.lower() or isinstance(getattr(e, 'reason', None), socket.timeout)
            if timeout_like:
                raise RetryableGeminiError(
                    f'Gemini network timeout: {reason}'
                )
            raise RuntimeError(f'Gemini request failed on {safe_endpoint}: {reason}')
        except Exception as e:
            raise RuntimeError(f'Gemini request failed on {safe_endpoint}: {e}')

        try:
            parsed = json.loads(raw)
        except Exception:
            raise RuntimeError('Gemini returned non-JSON response.')

        candidates = parsed.get('candidates') or []
        if not candidates:
            msg = parsed.get('error') or parsed
            raise RuntimeError(f'Gemini returned no candidates: {msg}')

        parts = ((candidates[0].get('content') or {}).get('parts')) or []
        text = ''.join((p.get('text') or '') for p in parts).strip()
        meta = {
            'candidates': len(candidates),
            'finish_reason': candidates[0].get('finishReason'),
        }
        return text, meta
    def _extract_book_text(self, book_id, title, max_words=120_000, char_budget=2_000_000):
        """
        Try to extract plain text from the book.
        Priority: TXT → EPUB → MOBI/AZW → PDF (first N chars).
        Returns a truncated string or empty string.
        """
        db = self.db

        # Preferred format order
        details = {
            'formats': [],
            'chosen_fmt': None,
            'path': None,
            'extractor': None,
            'error': None,
            'max_words': max_words,
            'char_budget': char_budget,
            'truncated': False,
            'word_count': 0,
            'source_word_count': 0,
        }

        formats = db.formats(book_id)
        if not formats:
            details['error'] = 'No formats found in Calibre metadata.'
            return '', details

        if isinstance(formats, str):
            formats = [f.strip() for f in formats.split(',') if f.strip()]
        else:
            formats = [str(f).strip() for f in formats if str(f).strip()]
        if not formats:
            details['error'] = 'Formats list was empty after parsing.'
            return '', details
        details['formats'] = formats

        format_priority = ['TXT', 'EPUB', 'MOBI', 'AZW3', 'AZW', 'PDF', 'HTML', 'RTF', 'LIT']
        formats_upper   = [f.upper() for f in formats]

        chosen_fmt = None
        for pref in format_priority:
            if pref in formats_upper:
                chosen_fmt = formats[formats_upper.index(pref)]
                break

        if not chosen_fmt:
            chosen_fmt = formats[0]
        details['chosen_fmt'] = chosen_fmt

        path = db.format_abspath(book_id, chosen_fmt)
        details['path'] = path
        if not path or not os.path.exists(path):
            details['error'] = f'Format path missing or not found for {chosen_fmt}.'
            return '', details

        fmt_upper = chosen_fmt.upper()

        try:
            extracted = ''
            if fmt_upper == 'TXT':
                details['extractor'] = 'plain-text reader'
                with open(path, 'r', errors='replace') as f:
                    extracted = f.read(char_budget)

            elif fmt_upper in ('EPUB',):
                details['extractor'] = 'EPUB HTML parser'
                extracted = self._extract_epub(path, char_budget)

            elif fmt_upper == 'PDF':
                details['extractor'] = 'PDF extractor'
                extracted = self._extract_pdf(path, char_budget)

            elif fmt_upper in ('MOBI', 'AZW3', 'AZW', 'LIT'):
                details['extractor'] = 'ebook-convert fallback'
                extracted = self._extract_mobi(path, char_budget)

            elif fmt_upper == 'HTML':
                details['extractor'] = 'HTML parser'
                extracted = self._extract_html_file(path, char_budget)

            else:
                # Generic: try reading as text
                details['extractor'] = 'generic text reader'
                with open(path, 'r', errors='replace') as f:
                    extracted = f.read(char_budget)

            cleaned = self._clean_extracted_text(extracted)
            final_text, was_truncated, final_words, source_words = self._truncate_to_words(cleaned, max_words)
            details['word_count'] = final_words
            details['source_word_count'] = source_words
            details['truncated'] = was_truncated
            return final_text, details
        except Exception as e:
            details['error'] = str(e)
            return '', details

    def _clean_extracted_text(self, text):
        if not text:
            return ''
        # Normalize converter artifacts so character counts better match real content.
        text = text.replace('\x00', ' ')
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'[ \t\f\v]+', ' ', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'\u00ad', '', text)  # soft hyphen
        return text.strip()

    def _truncate_to_words(self, text, max_words):
        if not text:
            return '', False, 0, 0
        words = text.split()
        source_words = len(words)
        if source_words <= max_words:
            return text, False, source_words, source_words
        return ' '.join(words[:max_words]), True, max_words, source_words

    def _extract_epub(self, path, max_chars):
        import zipfile
        from html.parser import HTMLParser

        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style'):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ('script', 'style'):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    self.text.append(data)

        result = []
        total  = 0
        try:
            with zipfile.ZipFile(path) as zf:
                names = sorted([n for n in zf.namelist() 
                                 if n.endswith(('.html', '.xhtml', '.htm'))])
                for name in names:
                    if total >= max_chars:
                        break
                    try:
                        data = zf.read(name).decode('utf-8', errors='replace')
                        parser = TextExtractor()
                        parser.feed(data)
                        chunk = ' '.join(parser.text)
                        result.append(chunk)
                        total += len(chunk)
                    except Exception:
                        continue
        except Exception:
            pass
        return ' '.join(result)[:max_chars]

    def _extract_pdf(self, path, max_chars):
        try:
            import pdfminer.high_level as pdfminer
            from io import StringIO
            out = StringIO()
            with open(path, 'rb') as f:
                pdfminer.extract_text_to_fp(f, out, output_type='text')
            return out.getvalue()[:max_chars]
        except ImportError:
            pass
        # Fallback: try calibre's own PDF extraction
        try:
            from calibre.ebooks.pdf.pdftohtml import pdftotext
            return pdftotext(path)[:max_chars]
        except Exception:
            return ''

    def _extract_mobi(self, path, max_chars):
        try:
            import tempfile, os
            # Convert to txt via calibre-debug
            with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as tmp:
                tmp_path = tmp.name
            kwargs = {
                'capture_output': True,
                'timeout': 120,
            }
            if os.name == 'nt':
                # Avoid flashing a terminal window for each conversion on Windows.
                kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.run(
                ['ebook-convert', path, tmp_path, '--output-profile=default'],
                **kwargs
            )
            if os.path.exists(tmp_path):
                with open(tmp_path, 'r', errors='replace') as f:
                    text = f.read(max_chars)
                os.unlink(tmp_path)
                return text
        except Exception:
            pass
        return ''

    def _extract_html_file(self, path, max_chars):
        from html.parser import HTMLParser

        class TP(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
            def handle_data(self, data):
                self.parts.append(data)

        with open(path, 'r', errors='replace') as f:
            raw = f.read(max_chars * 3)
        p = TP()
        p.feed(raw)
        return ' '.join(p.parts)[:max_chars]


# ─────────────────────────────────────────────
# Progress dialog
# ─────────────────────────────────────────────

class SummarizeJob(QDialog):
    """Dialog that shows progress and runs the summarization job."""

    def __init__(self, gui, book_ids):
        QDialog.__init__(self, gui)
        self.gui      = gui
        self.book_ids = book_ids
        self.db       = gui.current_db.new_api
        self.worker   = None
        self.failed_books = []

        self.setWindowTitle('Gemini Book Summarizer')
        self.setMinimumWidth(520)
        self.setMinimumHeight(300)

        layout = QVBoxLayout(self)

        self.status_label = QLabel('Initializing…')
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(len(book_ids))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(140)
        layout.addWidget(self.log)

        btn_row = QHBoxLayout()
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self._cancel)
        self.close_btn  = QPushButton('Close')
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def start(self):
        from calibre_plugins.gemini_summarizer.config import prefs

        self.show()

        self.worker = SummarizerWorker(
            db              = self.db,
            book_ids        = self.book_ids,
            api_key         = prefs['api_key'],
            model           = prefs['model'],
            prompt_template = prefs['prompt'],
            max_words       = prefs['max_words'],
            max_input_words = prefs['max_input_words'],
        )
        self.worker.progress.connect(self._on_progress)
        self.worker.book_done.connect(self._on_book_done)
        self.worker.book_error.connect(self._on_book_error)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, idx, msg):
        if msg and msg.strip():
            self.status_label.setText(msg)
        self.progress_bar.setValue(idx)
        self._log(msg)

    def _on_book_done(self, book_id, summary):
        from calibre_plugins.gemini_summarizer.config import prefs
        col = prefs['custom_column']
        mi  = self.db.get_metadata(book_id)
        title = mi.title

        try:
            # Write to custom column
            self.db.set_field(col, {book_id: summary})
            msg = f'✓ Summary saved for: {title}'
        except Exception as e:
            msg = f'✗ Saved to comments instead for "{title}" (column error: {e})'
            # Fallback: append to comments
            try:
                old_comments = mi.comments or ''
                new_comments = old_comments + f'\n\n--- Gemini Summary ---\n{summary}'
                self.db.set_field('comments', {book_id: new_comments})
            except Exception:
                pass

        self._log(msg)
        self.progress_bar.setValue(self.progress_bar.value() + 1)

    def _on_book_error(self, book_id, error):
        if book_id == -1:
            self._log(f'FATAL ERROR:\n{error}')
        else:
            try:
                mi    = self.db.get_metadata(book_id)
                title = mi.title
            except Exception:
                title = f'book_id={book_id}'
            self.failed_books.append(title)
            self._log(f'✗ Error for "{title}":\n{error}')
        self.progress_bar.setValue(self.progress_bar.value() + 1)

    def _on_finished(self):
        if self.failed_books:
            self.status_label.setText(f'Done with errors ({len(self.failed_books)} failed).')
        else:
            self.status_label.setText('Done!')
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self._log('\n─── All done ───')
        if self.failed_books:
            self._log(f'⚠ Failed books: {len(self.failed_books)}')
            for title in self.failed_books:
                self._log(f'  - {title}')
        # Refresh Calibre's book list
        try:
            self.gui.iactions['Edit Metadata'].refresh_books_after_metadata_edit(
                set(self.book_ids)
            )
        except Exception:
            try:
                self.gui.current_view().model().refresh()
            except Exception:
                pass

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self._log('Cancellation requested…')

    def _log(self, msg):
        self.log.append(msg)
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())
