# ---
# jupyter:
#   jupytext:
#     formats: py:percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # Speech feature extraction — starter notebook
#
# Extracts standard acoustic features from voice recordings, one row per recording:
#
# - **Pitch (F0):** mean, SD, range — via `praat-parselmouth`
# - **Voice quality:** jitter, shimmer, harmonics-to-noise ratio (HNR) — via `praat-parselmouth`
# - **Formants:** mean F1–F3 — via `praat-parselmouth`
# - **Timing:** speech/pause durations, pause count, phonation ratio — via `librosa`
# - **MFCCs:** mean, SD, skewness, kurtosis of 13 coefficients — via `librosa` + `scipy`
#
# **Scope:** this is feature extraction only. It is *not* a validated clinical model; feature definitions
# and parameters here will not exactly match any published pipeline (published speech-based HE models
# have not released their code). All parameters live in one `PARAMS` dict and are saved alongside the
# output so every run is reproducible.
#
# > 🔒 **Privacy:** voice recordings are identifiable. Keep them outside the repository (see `AUDIO_DIR`
# > below); recordings, feature CSVs and `.ipynb` copies with outputs are git-ignored and must never be
# > committed. The hosted dashboards only show this code read-only; it doesn't run there or in JupyterLite
# > (Praat doesn't run in the browser).

# %%
# Run once per environment (or add to the kernel image requirements)
# %pip install -r ../requirements-speech.txt
# mp3/m4a files also need ffmpeg installed on the system (e.g. `brew install ffmpeg`).

# %%
import json
import os
from pathlib import Path
import warnings

import numpy as np
import pandas as pd
import librosa
import parselmouth
from parselmouth.praat import call
from scipy.stats import skew, kurtosis

print("parselmouth", parselmouth.__version__, "| librosa", librosa.__version__)

# %% [markdown]
# ## Parameters
#
# Change these here, not inside the functions. Notes:
# - **Pitch floor/ceiling:** Praat's common defaults are 75–300 Hz for male voices and 100–500 Hz for female voices.
#   A single wide range (75–500) works for a first pass but adds pitch-tracking errors.
# - **`silence_top_db`:** frames quieter than (peak − this many dB) count as silence. Raise it for noisy recordings.
# - **`min_pause_s`:** gaps shorter than this aren't counted as pauses (0.25 s is a common choice).
#
# **Where files go:** recordings are read from `HE_SPEECH_DIR` (default: a `speech recordings` folder next to
# the repository, like the study data folder). Results are written to `notebook_outputs/` (git-ignored), or to
# `HE_SPEECH_OUTPUT_DIR` if set.

# %%
PARAMS = {
    "sample_rate": 16000,      # all audio resampled to this, mono
    "pitch_floor": 75.0,       # Hz
    "pitch_ceiling": 500.0,    # Hz
    "max_formant": 5500.0,     # Hz (5000 typical for male, 5500 for female)
    "silence_top_db": 30.0,    # dB below peak treated as silence
    "min_pause_s": 0.25,       # shortest gap counted as a pause
    "n_mfcc": 13,
}

_REPO = Path.cwd().parent if Path.cwd().name == "notebooks" else Path.cwd()
AUDIO_DIR = Path(os.environ.get("HE_SPEECH_DIR", _REPO.parent / "speech recordings")).expanduser()
OUTPUT_DIR = Path(os.environ.get("HE_SPEECH_OUTPUT_DIR", _REPO / "notebook_outputs")).expanduser()
OUTPUT_CSV = OUTPUT_DIR / "speech_features.csv"
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".ogg"}  # mp3/m4a need ffmpeg installed

# %% [markdown]
# ## Feature functions

# %%
def load_audio(path, sr):
    """Load any supported file as mono float32 at a fixed sample rate."""
    y, _ = librosa.load(path, sr=sr, mono=True)
    return y


def praat_features(y, sr, p):
    """Pitch, jitter, shimmer, HNR and formants via Praat (parselmouth)."""
    snd = parselmouth.Sound(y.astype(np.float64), sampling_frequency=sr)
    out = {}

    pitch = snd.to_pitch(pitch_floor=p["pitch_floor"], pitch_ceiling=p["pitch_ceiling"])
    f0 = pitch.selected_array["frequency"]
    f0 = f0[f0 > 0]  # voiced frames only
    out["f0_mean_hz"] = f0.mean() if f0.size else np.nan
    out["f0_sd_hz"] = f0.std() if f0.size else np.nan
    out["f0_range_hz"] = (np.percentile(f0, 95) - np.percentile(f0, 5)) if f0.size else np.nan
    out["voiced_fraction"] = f0.size / max(pitch.get_number_of_frames(), 1)

    pp = call(snd, "To PointProcess (periodic, cc)", p["pitch_floor"], p["pitch_ceiling"])
    out["jitter_local"] = call(pp, "Get jitter (local)", 0, 0, 0.0001, 0.02, 1.3)
    out["jitter_rap"] = call(pp, "Get jitter (rap)", 0, 0, 0.0001, 0.02, 1.3)
    out["shimmer_local"] = call([snd, pp], "Get shimmer (local)", 0, 0, 0.0001, 0.02, 1.3, 1.6)
    out["shimmer_apq3"] = call([snd, pp], "Get shimmer (apq3)", 0, 0, 0.0001, 0.02, 1.3, 1.6)

    harm = call(snd, "To Harmonicity (cc)", 0.01, p["pitch_floor"], 0.1, 1.0)
    out["hnr_db"] = call(harm, "Get mean", 0, 0)

    formant = call(snd, "To Formant (burg)", 0.0, 5, p["max_formant"], 0.025, 50)
    for i in (1, 2, 3):
        out[f"f{i}_mean_hz"] = call(formant, "Get mean", i, 0, 0, "hertz")

    return out


def timing_features(y, sr, p):
    """Speech/pause timing from an energy-based silence split."""
    total_s = len(y) / sr
    intervals = librosa.effects.split(y, top_db=p["silence_top_db"])  # non-silent [start, end] samples
    speech_s = sum(e - s for s, e in intervals) / sr
    gaps = [(intervals[i + 1][0] - intervals[i][1]) / sr for i in range(len(intervals) - 1)]
    pauses = [g for g in gaps if g >= p["min_pause_s"]]
    return {
        "duration_s": total_s,
        "speech_s": speech_s,
        "phonation_ratio": speech_s / total_s if total_s else np.nan,
        "pause_count": len(pauses),
        "pause_rate_per_min": len(pauses) / (total_s / 60) if total_s else np.nan,
        "pause_mean_s": np.mean(pauses) if pauses else 0.0,
        "pause_total_s": float(np.sum(pauses)),
    }


def mfcc_features(y, sr, p):
    """MFCC summary statistics across frames (mean, SD, skewness, kurtosis)."""
    m = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=p["n_mfcc"])
    out = {}
    for i, row in enumerate(m):
        out[f"mfcc{i}_mean"] = row.mean()
        out[f"mfcc{i}_sd"] = row.std()
        out[f"mfcc{i}_skew"] = skew(row)
        out[f"mfcc{i}_kurt"] = kurtosis(row)
    return out


def extract_features(path, params=PARAMS):
    """All features for one recording. Errors are recorded rather than raised."""
    row = {"file": Path(path).name, "error": None}
    try:
        y = load_audio(path, params["sample_rate"])
        sr = params["sample_rate"]
        row.update(timing_features(y, sr, params))
        row.update(praat_features(y, sr, params))
        row.update(mfcc_features(y, sr, params))
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
    return row

# %% [markdown]
# ## Test on a single file

# %%
files = sorted(f for f in AUDIO_DIR.rglob("*") if f.suffix.lower() in AUDIO_EXTS) if AUDIO_DIR.is_dir() else []
HAVE_FILES = bool(files)
print(f"{len(files)} audio files found in {AUDIO_DIR}/")

if not HAVE_FILES:
    print(f"No recordings yet. To add some: put audio files ({', '.join(sorted(AUDIO_EXTS))}) in\n"
          f"  {AUDIO_DIR}\n"
          "or point HE_SPEECH_DIR at another folder, then restart the kernel and Run All.\n"
          "mp3/m4a files need ffmpeg installed. Keep recordings outside the repository: they are identifiable.\n"
          "The cells below skip until recordings exist.")

if HAVE_FILES:
    display(pd.Series(extract_features(files[0])).to_frame("value"))

# %% [markdown]
# ## Run on all recordings and save

# %%
if HAVE_FILES:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        rows = [extract_features(f) for f in files]

    df = pd.DataFrame(rows)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_CSV, index=False)
    OUTPUT_CSV.with_suffix(".params.json").write_text(json.dumps(PARAMS, indent=2))

    print(f"Saved {len(df)} rows x {df.shape[1]} columns to {OUTPUT_CSV}")
    print(f"Failed: {df['error'].notna().sum()}")
    display(df[df["error"].notna()][["file", "error"]])
else:
    print("Skipped: no recordings.")

# %% [markdown]
# ## Sanity checks
#
# Look for implausible values before using any of this: F0 means outside the pitch range,
# HNR that is negative or NaN (often noisy audio or very little voicing), or a phonation ratio near 0 or 1
# (silence threshold set wrong for that recording).

# %%
key_cols = ["duration_s", "phonation_ratio", "pause_rate_per_min", "f0_mean_hz", "f0_sd_hz",
            "jitter_local", "shimmer_local", "hnr_db"]
# Columns can be missing when every recording failed (only "file" and "error" are filled then).
have_cols = HAVE_FILES and all(c in df.columns for c in key_cols)
if have_cols:
    display(df[key_cols].describe().T)
else:
    print("Skipped: no feature values to check.")

# %%
import matplotlib.pyplot as plt

if have_cols:
    fig, axes = plt.subplots(2, 4, figsize=(14, 6))
    for ax, col in zip(axes.flat, key_cols):
        df[col].dropna().plot.hist(ax=ax, bins=20)
        ax.set_title(col)
    plt.tight_layout()
else:
    print("Skipped: no feature values to plot.")

# %% [markdown]
# ## Next steps (not done here)
#
# - Join `speech_features.csv` to clinical data by participant/visit ID.
# - Record task type, device and setting for each recording as metadata columns.
# - If a collaborator shares a protocol or feature spec, adjust `PARAMS` to match it and note the source.
