#!/usr/bin/env python3
"""Lab OCR worker: RapidOCR over scan pages, resumable per-book.
Runs in a thread; renders with PyMuPDF at `dpi`, detects text capped at
960px long side (prod perf trick), writes rows via store.Writer.
Stop flag is cooperative: finishes current page, keeps done books.
"""
import os, time, threading
import years
import ingest


def enable_cuda():
    """Put the pip-installed CUDA runtime on the DLL search path.

    `pip install nvidia-cudnn-cu12 ...` drops the DLLs under
    site-packages/nvidia/<pkg>/bin, which onnxruntime's LoadLibrary calls do not
    search. Without this, CUDAExecutionProvider silently falls back to the CPU.
    Returns the list of directories that were made visible.
    """
    if os.environ.get("WOOD_OCR_PROVIDERS", "").strip().lower().startswith("cpu"):
        return []
    try:
        import site
    except ImportError:
        return []
    dirs = []
    for base in site.getsitepackages():
        nvidia = os.path.join(base, "nvidia")
        if not os.path.isdir(nvidia):
            continue
        for sub in ("cudnn", "cublas", "cuda_runtime", "cu13", "cuda_nvrtc"):
            b = os.path.join(nvidia, sub, "bin")
            if os.path.isdir(b):
                dirs.append(b)
    for b in dirs:
        os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(b)
        except (OSError, AttributeError):
            pass
    return dirs


def _patch_rapidocr_cuda_flag():
    """rapidocr 1.2.3 bug: `rec_use_cuda` never reaches the session.

    `UpdateParameters.update_rec_params` strips the `rec_` prefix from
    `rec_model_path` only, so `rec_use_cuda=True` is stored under the literal key
    `rec_use_cuda` and `OrtInferSession` never sees `use_cuda`. The recogniser is
    ~96% of the work, so the whole engine silently stayed on the CPU while the
    error-free warning said nothing. Det has the opposite (correct) behaviour.
    Fix: strip the prefix from every rec_* key. Idempotent.
    """
    try:
        from rapidocr_onnxruntime.utils import UpdateParameters
    except Exception:
        return False
    if getattr(UpdateParameters.update_rec_params, "_wood_patched", False):
        return True

    def update_rec_params(self, config, rec_dict):
        if rec_dict:
            nd = {}
            for k, v in rec_dict.items():
                nd[k.split("rec_")[1] if k.startswith("rec_") else k] = v
            rec_dict = nd
            if not rec_dict.get("model_path"):
                rec_dict["model_path"] = config["model_path"]
            config.update(rec_dict)
        return config

    update_rec_params._wood_patched = True
    UpdateParameters.update_rec_params = update_rec_params
    return True


def ocr_providers():
    """Which execution providers to request.

    Empty means "leave on rapidocr's default", which is CPU-only. On the RTX
    3080 the GPU is ~8x faster (6.5 vs 0.8 pages/s, measured over 12 pages of
    the same book), so it is the default whenever a real CUDA session can be
    created. Set WOOD_OCR_PROVIDERS=CPU to force the CPU.
    """
    env = os.environ.get("WOOD_OCR_PROVIDERS", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    return ["CUDAExecutionProvider"] if cuda_available() else []


_cuda_ok = None


def cuda_available():
    """True only if a CUDA session can really be created (memoised).

    The probe loads a model, so it costs ~1s; it is asked for at server start,
    by ocr_providers() and by Health, and must not repeat that.
    """
    global _cuda_ok
    if _cuda_ok is None:
        _cuda_ok = _cuda_probe()
    return _cuda_ok


def _cuda_probe():
    """True only if a CUDA session can really be created."""
    if not enable_cuda():
        return False
    try:
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            return False
    except Exception:
        return False
    import tempfile
    # a real (tiny) model, so a missing cuDNN fails here and not mid-book
    try:
        import rapidocr_onnxruntime as r
        det = os.path.join(os.path.dirname(r.__file__), "models",
                           "ch_PP-OCRv3_det_infer.onnx")
        if not os.path.exists(det):
            return False
        import onnxruntime as ort
        s = ort.InferenceSession(det, providers=["CUDAExecutionProvider"])
        return "CUDAExecutionProvider" in s.get_providers()
    except Exception:
        return False

class OcrJob:
    def __init__(self, writer, on_event, dpi=150):
        self.writer = writer
        self.on_event = on_event
        self.dpi = dpi
        self.stop = threading.Event()
        self.thread = None
        self.current = None
        self._engines = {}      # cache so each model is built once, not per book
        self.cuda = "CUDAExecutionProvider" in ocr_providers()

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, targets):
        if self.running():
            return False
        self.stop.clear()
        self.thread = threading.Thread(target=self._run, args=(targets,), daemon=True)
        self.thread.start()
        return True

    def request_stop(self):
        self.stop.set()

    def _engine(self):
        providers = tuple(ocr_providers())
        if providers:
            enable_cuda()          # must run before the session is created
        hit = self._engines.get(providers)
        if hit is not None:
            return hit
        import rapidocr_onnxruntime as r
        from rapidocr_onnxruntime import RapidOCR
        m = os.path.join(os.path.dirname(r.__file__), "models")
        det = os.path.join(m, "ch_PP-OCRv3_det_infer.onnx")
        kw = dict(det_model_path=det, det_limit_side_len=960, det_limit_type="max")
        if providers:
            # rapidocr 1.2.3 drops each section's default model_path unless it
            # is passed next to the use_cuda flag, so pass them all.
            kw["rec_model_path"] = os.path.join(m, "ch_PP-OCRv3_rec_infer.onnx")
            kw["cls_model_path"] = os.path.join(m, "ch_ppocr_mobile_v2.0_cls_infer.onnx")
            if "CUDAExecutionProvider" in providers:
                _patch_rapidocr_cuda_flag()
                # cls_use_cuda crashes 1.2.3 (KeyError: model_path); the angle
                # classifier is negligible work, so it stays on the CPU.
                kw["det_use_cuda"] = True
                kw["rec_use_cuda"] = True
        try:
            eng = RapidOCR(**kw)
        except Exception as e:
            raise RuntimeError(f"OCR engine init failed: {e}")
        mode = "CPU"
        if providers:
            try:
                # the detector keeps its session in `.infer`, the recogniser in
                # `.session`; both wrap the real one one level down
                got = []
                for holder, attr in ((eng.text_recognizer, "session"),
                                     (eng.text_detector, "infer")):
                    sess = getattr(getattr(holder, attr, None), "session", None)
                    if sess is not None:
                        got += sess.get_providers()
                mode = "GPU" if "CUDAExecutionProvider" in got else "CPU"
            except Exception:
                mode = "CPU"
        self._engines[providers] = (eng, mode)
        return eng, mode

    def _ocr_book(self, path, title):
        import fitz
        engine, mode = self._engine()
        doc = fitz.open(path)
        n = doc.page_count
        mat = fitz.Matrix(self.dpi / 72.0, self.dpi / 72.0)
        rows, texts = [], []
        t0 = time.time()
        for i in range(n):
            if self.stop.is_set():
                doc.close()
                return None, "stopped"
            pix = doc[i].get_pixmap(matrix=mat)
            import numpy as np
            img = np.frombuffer(pix.samples, dtype="uint8").reshape(
                pix.height, pix.width, pix.n)
            res, _ = engine(img)
            txt = " ".join(seg[1] for seg in (res or []) if seg and len(seg) > 1)
            texts.append(txt)
            if len(txt.strip()) >= 25:
                rows.append((txt, ingest.collection_for(path), title, str(i + 1),
                             os.path.abspath(path)))
            if (i + 1) % 20 == 0 or i + 1 == n:
                self.on_event("ocr_running", title,
                              f"p.{i + 1}/{n} ({mode})")
        doc.close()
        return (rows, texts, time.time() - t0, mode), None

    def _run(self, targets):
        total = len(targets)
        for k, (path, title) in enumerate(targets, 1):
            if self.stop.is_set():
                break
            self.current = title
            self.on_event("ocr_running", title, f"book {k}/{total}")
            try:
                out, err = self._ocr_book(path, title)
            except Exception as e:
                out, err = None, str(e)
            if out is None:
                self.on_event("image-only", title, f"OCR stopped ({err})")
                continue
            rows, texts, dt, mode = out
            abspath = os.path.abspath(path)
            y = years.book_year(title, " ".join(texts[:6]), " ".join(texts[-4:]))
            def _save(c, _rows=rows, _y=y):
                c.executemany("INSERT INTO pages(text,collection,title,page,path)"
                              " VALUES(?,?,?,?,?)", _rows)
                rids = [r[0] for r in c.execute(
                    "SELECT rowid FROM pages WHERE path=?", (abspath,))]
                c.executemany("INSERT OR REPLACE INTO pyear VALUES(?,?)",
                              [(r, _y) for r in rids])
                npages = len(texts)
                c.execute("UPDATE sources SET indexed_pages=?, chars=?, status='ocr'"
                          " WHERE path=?", (len(_rows), sum(len(t) for t in texts), abspath))
                c.execute("INSERT OR REPLACE INTO jobs(path,title,status,detail,updated)"
                          " VALUES(?,?,?,?,?)",
                          (abspath, title, "done",
                           f"OCR {mode}: {len(_rows)}/{npages}p in {dt:.0f}s year={_y}",
                           time.time()))
            self.writer.submit(_save)
            self.on_event("done", title, f"OCR {mode}: {len(rows)}p in {dt:.0f}s")
        self.current = None
        self.on_event("idle", "-", "OCR queue idle")
