"""
UDP Transport Stream output.

Bevorzugter Backend: GStreamer appsrc → x264enc → mpegtsmux → udpsink
Fallback:           FFmpeg stdin-Pipe (bisheriges Verhalten)

GStreamer läuft in einem eigenen Thread. Python schreibt Frames in eine
Queue; der GStreamer-Thread schiebt sie per appsrc in die Pipeline.
Das Encoding (x264) und Senden (UDP) laufen vollständig entkoppelt vom
Tracking-Loop.
"""

import logging
import queue
import threading
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

_GST_AVAILABLE = False
try:
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GLib", "2.0")
    from gi.repository import Gst, GLib
    Gst.init(None)
    _GST_AVAILABLE = True
except Exception:
    pass


# ---------------------------------------------------------------------------
# GStreamer backend
# ---------------------------------------------------------------------------

class _GstStreamer:
    """
    GStreamer appsrc pipeline:
      appsrc → videoconvert → x264enc → mpegtsmux → udpsink
    """

    def __init__(self, host, port, width, height, fps, bitrate_kbps, preset, ttl):
        self.width  = width
        self.height = height
        self.fps    = fps

        self._queue: queue.Queue = queue.Queue(maxsize=4)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[GLib.MainLoop] = None
        self._pipeline = None
        self._appsrc   = None
        self._pts      = 0
        self._duration = int(Gst.SECOND / fps)

        # Build pipeline string
        self._pipe_str = (
            f"appsrc name=src is-live=true block=false format=time "
            f"caps=video/x-raw,format=BGR,width={width},height={height},"
            f"framerate={int(fps)}/1 ! "
            f"videoconvert ! "
            f"x264enc tune=zerolatency bitrate={bitrate_kbps} "
            f"speed-preset={preset} key-int-max={int(fps*2)} ! "
            f"mpegtsmux ! "
            f"udpsink host={host} port={port} ttl={ttl} sync=false"
        )

    def start(self) -> bool:
        try:
            self._pipeline = Gst.parse_launch(self._pipe_str)
        except Exception as e:
            logger.error(f"GStreamer pipeline parse error: {e}")
            return False

        self._appsrc = self._pipeline.get_by_name("src")
        if self._appsrc is None:
            logger.error("GStreamer: could not get appsrc element")
            return False

        self._pipeline.set_state(Gst.State.PLAYING)

        # GLib main loop runs in background thread to handle GStreamer bus
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gst-loop")
        self._thread.start()

        logger.info(f"GStreamer UDP-Stream gestartet → {self._pipe_str.split('udpsink')[1].strip()}")
        logger.debug(f"GStreamer pipeline: {self._pipe_str}")
        return True

    def _run(self):
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)
        self._loop.run()

    def _on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        logger.error(f"GStreamer error: {err}  {dbg}")
        self._loop.quit()

    def write_frame(self, frame: np.ndarray) -> bool:
        if self._appsrc is None:
            return False
        # Drop frame if queue full (non-blocking)
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            return True  # drop silently — streamer is slower than tracker

        # Push all queued frames into appsrc
        while True:
            try:
                f = self._queue.get_nowait()
            except queue.Empty:
                break
            self._push(f)
        return True

    def _push(self, frame: np.ndarray):
        data = frame.tobytes()
        buf  = Gst.Buffer.new_allocate(None, len(data), None)
        buf.fill(0, data)
        buf.pts      = self._pts
        buf.duration = self._duration
        self._pts   += self._duration
        self._appsrc.emit("push-buffer", buf)

    def stop(self):
        if self._pipeline:
            self._appsrc.emit("end-of-stream")
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("GStreamer UDP-Stream gestoppt")


# ---------------------------------------------------------------------------
# FFmpeg fallback backend
# ---------------------------------------------------------------------------

class _FfmpegStreamer:
    """FFmpeg stdin-Pipe fallback (original behaviour)."""

    def __init__(self, host, port, width, height, fps, bitrate, preset, ttl):
        self._host    = host
        self._port    = port
        self._width   = width
        self._height  = height
        self._fps     = fps
        self._bitrate = bitrate
        self._preset  = preset
        self._ttl     = ttl
        self._proc    = None

    def start(self) -> bool:
        import shutil, subprocess
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg nicht gefunden: sudo apt install ffmpeg")
            return False

        url = f"udp://{self._host}:{self._port}?ttl={self._ttl}&pkt_size=1316"
        cmd = [
            "ffmpeg", "-loglevel", "warning",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self._width}x{self._height}",
            "-r", str(self._fps), "-i", "pipe:0",
            "-c:v", "libx264", "-preset", self._preset,
            "-tune", "zerolatency", "-b:v", self._bitrate,
            "-maxrate", self._bitrate, "-bufsize", "500k",
            "-g", str(int(self._fps * 2)),
            "-pix_fmt", "yuv420p", "-f", "mpegts", url,
        ]
        logger.info(f"Starte FFmpeg UDP-Stream → {url}  "
                    f"({self._width}x{self._height} {self._fps}fps {self._bitrate})")
        logger.debug("FFmpeg-Kommando: " + " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                      stdout=subprocess.DEVNULL, stderr=None)
        return True

    def write_frame(self, frame: np.ndarray) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return False
        try:
            self._proc.stdin.write(frame.tobytes())
            return True
        except BrokenPipeError:
            logger.error("FFmpeg-Pipe unterbrochen")
            return False

    def stop(self):
        if self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None
        logger.info("FFmpeg UDP-Stream gestoppt")


# ---------------------------------------------------------------------------
# Public API — chooses backend automatically
# ---------------------------------------------------------------------------

class UDPStreamer:
    """
    Schreibt BGR-Frames als MPEG-TS per UDP.

    Verwendet GStreamer (appsrc) wenn verfügbar, sonst FFmpeg.
    """

    def __init__(self,
                 host: str,
                 port: int,
                 width: int,
                 height: int,
                 fps: float = 25.0,
                 bitrate: str = "4M",
                 preset: str = "ultrafast",
                 ttl: int = 64):
        # Parse bitrate string (e.g. "4M" → 4000 kbps for GStreamer)
        br_str = bitrate.upper().rstrip("B")
        if br_str.endswith("M"):
            bitrate_kbps = int(float(br_str[:-1]) * 1000)
        elif br_str.endswith("K"):
            bitrate_kbps = int(br_str[:-1])
        else:
            bitrate_kbps = int(br_str) // 1000

        if _GST_AVAILABLE:
            self._backend = _GstStreamer(host, port, width, height, fps,
                                         bitrate_kbps, preset, ttl)
            logger.debug("UDP-Stream backend: GStreamer")
        else:
            self._backend = _FfmpegStreamer(host, port, width, height, fps,
                                            bitrate, preset, ttl)
            logger.debug("UDP-Stream backend: FFmpeg (GStreamer nicht verfügbar)")

    def start(self) -> bool:
        return self._backend.start()

    def write_frame(self, frame: np.ndarray) -> bool:
        return self._backend.write_frame(frame)

    def stop(self):
        self._backend.stop()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
