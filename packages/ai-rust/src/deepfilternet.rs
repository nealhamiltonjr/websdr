//! DeepFilterNet wrapper — Stage 2a of the DSP+AI cascade.
//!
//! Once the upstream DeepFilterNet crate is wired (slice-2), this module
//! exposes:
//!   - `Denoiser::load_model(path)` → load a `.ckpt`
//!   - `Denoiser::process(samples)` → denoise one frame
//!   - GPU detection (CUDA via `cudarc`, Metal via `metal-rs`)
//!
//! Slice-1 status: declarations only.

use std::path::Path;

/// A loaded DeepFilterNet model.
pub struct Denoiser {
    /// Frame size (samples per call). DeepFilterNet uses 480 (RNNoise-compatible).
    pub frame_size: usize,
    // TODO(slice-2): hold `deepfilter::DenoiseState` here
}

impl Denoiser {
    /// Load a DeepFilterNet checkpoint from disk.
    pub fn load_model(_path: &Path) -> Result<Self, DeepFilterError> {
        Err(DeepFilterError::NotImplemented)
    }

    /// Process one frame of audio. `samples.len()` must equal `frame_size`.
    pub fn process(&self, samples: &mut [f32]) -> Result<(), DeepFilterError> {
        if samples.len() != self.frame_size {
            return Err(DeepFilterError::WrongFrameSize);
        }
        // TODO(slice-2): real inference here
        Ok(())
    }
}

#[derive(Debug)]
pub enum DeepFilterError {
    NotImplemented,
    WrongFrameSize,
}

impl std::fmt::Display for DeepFilterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotImplemented => write!(f, "not implemented in slice-1 stub"),
            Self::WrongFrameSize => write!(f, "wrong frame size"),
        }
    }
}

impl std::error::Error for DeepFilterError {}
