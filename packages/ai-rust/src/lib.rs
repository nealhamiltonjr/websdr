//! OpenWebRX+ AI inference module (Rust).
//!
//! Stage 2a of the DSP+AI cascade (see ADR-002): server-side DeepFilterNet
//! noise reduction on the demodulated audio path.
//!
//! Slice-18: the `Denoiser` struct in `deepfilternet.rs` is now a REAL
//! spectral-subtraction denoiser (mirrors the algorithm of
//! `apps/server/openwebrx_plus/dsp/ai_denoise.py`), not a stub. The
//! `process` function below is the FFI surface — callable from Python
//! via `ctypes` once the cdylib is built.
//!
//! Future slices will swap the spectral-subtraction body of
//! `Denoiser::process_frame` for a DeepFilterNet inference call once
//! the upstream `deepfilter` crate is wired (model loading is the
//! gating item; `DeepFilterError::NotImplemented` is the sentinel).

pub mod deepfilternet;

pub use deepfilternet::{DeepFilterError, Denoiser, DenoiserConfig};

/// Library version string.
#[no_mangle]
pub extern "C" fn owrx_ai_version() -> *const std::os::raw::c_char {
    static VERSION: &[u8] = b"openwebrx-plus ai 0.1.0 (slice-18 spectral-subtraction)\0";
    VERSION.as_ptr() as *const std::os::raw::c_char
}

/// Smoke test: returns the sum of two i32 — proves the C ABI works.
#[no_mangle]
pub extern "C" fn owrx_ai_add(a: i32, b: i32) -> i32 {
    a + b
}

/// Process a frame of audio samples through the spectral-subtraction
/// denoiser. FFI surface for Python (via ctypes).
///
/// # Safety
/// - `samples_ptr` must point to a slice of `frame_size` f32 values.
/// - The denoiser must have been initialized with `frame_size` matching
///   the buffer length (otherwise the call returns -1 and the buffer
///   is left unchanged).
///
/// # Returns
/// - 0 on success
/// - -1 on wrong frame size
/// - -2 on null pointer
#[no_mangle]
pub extern "C" fn owrx_ai_denoise_frame(
    denoiser_ptr: *mut std::os::raw::c_void,
    samples_ptr: *mut f32,
    samples_len: usize,
) -> i32 {
    if denoiser_ptr.is_null() || samples_ptr.is_null() {
        return -2;
    }
    // SAFETY: caller guarantees denoiser_ptr points to a valid
    // Denoiser created by owrx_ai_denoiser_new (below).
    let denoiser: &mut Denoiser = unsafe {
        &mut *(denoiser_ptr as *mut Denoiser)
    };
    // SAFETY: caller guarantees samples_ptr points to samples_len
    // f32 values valid for read+write.
    let samples: &mut [f32] = unsafe {
        std::slice::from_raw_parts_mut(samples_ptr, samples_len)
    };
    match denoiser.process_frame(samples) {
        Ok(()) => 0,
        Err(DeepFilterError::WrongFrameSize) => -1,
        Err(_) => -3,
    }
}

/// Create a new Denoiser with default config (frame_size=480, fft_size=512).
/// Returns a heap-allocated pointer; caller MUST free with
/// `owrx_ai_denoiser_free`.
///
/// # Returns
/// - On success: a non-null pointer to a `Denoiser`.
/// - On failure (invalid config): null.
#[no_mangle]
pub extern "C" fn owrx_ai_denoiser_new(frame_size: usize) -> *mut std::os::raw::c_void {
    let cfg = DenoiserConfig {
        frame_size,
        ..Default::default()
    };
    match Denoiser::new(cfg) {
        Ok(d) => Box::into_raw(Box::new(d)) as *mut std::os::raw::c_void,
        Err(_) => std::ptr::null_mut(),
    }
}

/// Free a Denoiser created by `owrx_ai_denoiser_new`.
///
/// # Safety
/// `ptr` must point to a Denoiser created by `owrx_ai_denoiser_new`,
/// and must not have been freed already (use-after-free is UB).
#[no_mangle]
pub extern "C" fn owrx_ai_denoiser_free(ptr: *mut std::os::raw::c_void) {
    if ptr.is_null() {
        return;
    }
    // SAFETY: caller guarantees ptr was created by owrx_ai_denoiser_new
    // and is being freed exactly once.
    unsafe {
        let _ = Box::from_raw(ptr as *mut Denoiser);
    }
}

/// Reset a Denoiser's streaming state (clears noise floor + overlap
/// buffers). Idempotent; safe to call between sessions.
///
/// # Safety
/// `ptr` must point to a valid Denoiser.
#[no_mangle]
pub extern "C" fn owrx_ai_denoiser_reset(ptr: *mut std::os::raw::c_void) {
    if ptr.is_null() {
        return;
    }
    // SAFETY: caller guarantees ptr is a valid Denoiser.
    unsafe {
        let denoiser: &mut Denoiser = &mut *(ptr as *mut Denoiser);
        denoiser.reset();
    }
}

/// Process a frame of audio samples through the denoiser.
///
/// Convenience wrapper for Rust callers (not the C ABI surface —
/// that's `owrx_ai_denoise_frame`). Returns the denoised samples;
/// for in-place mutation use `Denoiser::process_frame` directly.
pub fn process(samples: &[f32]) -> Vec<f32> {
    // For non-streaming one-shot use, create a default denoiser,
    // process one frame, return the result. This matches the
    // slice-1 stub API (samples in, samples out).
    let mut d = match Denoiser::new(DenoiserConfig::default()) {
        Ok(d) => d,
        Err(_) => return samples.to_vec(),
    };
    let frame_size = d.frame_size();
    let mut buf = samples.to_vec();
    // Pad or truncate to frame_size.
    buf.resize(frame_size, 0.0);
    if d.process_frame(&mut buf).is_err() {
        return samples.to_vec();
    }
    // Return only the new hop_size samples (the rest is the next
    // frame's input overlap that the caller would overwrite).
    let hop = d.config.hop_size.min(buf.len());
    buf[..hop].to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn smoke_add() {
        assert_eq!(owrx_ai_add(2, 3), 5);
    }

    #[test]
    fn version_is_non_null() {
        let v = owrx_ai_version();
        assert!(!v.is_null());
        // SAFETY: VERSION static is null-terminated; reading up to the
        // null is sound.
        unsafe {
            let cstr = std::ffi::CStr::from_ptr(v);
            let s = cstr.to_str().unwrap();
            assert!(s.contains("openwebrx-plus"));
            assert!(s.contains("slice-18"));
        }
    }

    #[test]
    fn denoiser_new_default_succeeds() {
        let d = owrx_ai_denoiser_new(480);
        assert!(!d.is_null());
        owrx_ai_denoiser_free(d);
    }

    #[test]
    fn denoiser_new_zero_frame_fails() {
        // frame_size=0 → fft_size=512 ≥ 0, hop_size=240 > 0, but
        // many internal allocations fail with size 0 → InvalidConfig.
        let d = owrx_ai_denoiser_new(0);
        assert!(d.is_null());
    }

    #[test]
    fn denoiser_free_null_is_safe() {
        // Calling free on null must not crash.
        owrx_ai_denoiser_free(std::ptr::null_mut());
    }

    #[test]
    fn denoise_frame_round_trip() {
        let d = owrx_ai_denoiser_new(480);
        assert!(!d.is_null());
        let mut samples = vec![0.0_f32; 480];
        for i in 0..480 {
            samples[i] = 0.5 * (2.0 * std::f32::consts::PI * 1000.0 * i as f32 / 8000.0).sin();
        }
        let rc = owrx_ai_denoise_frame(d, samples.as_mut_ptr(), samples.len());
        assert_eq!(rc, 0);
        owrx_ai_denoiser_free(d);
    }

    #[test]
    fn denoise_frame_wrong_size_returns_minus_one() {
        let d = owrx_ai_denoiser_new(480);
        let mut bad = vec![0.0_f32; 100];
        let rc = owrx_ai_denoise_frame(d, bad.as_mut_ptr(), bad.len());
        assert_eq!(rc, -1);
        owrx_ai_denoiser_free(d);
    }

    #[test]
    fn denoise_frame_null_ptr_returns_minus_two() {
        let rc = owrx_ai_denoise_frame(std::ptr::null_mut(), std::ptr::null_mut(), 0);
        assert_eq!(rc, -2);
    }

    #[test]
    fn process_returns_vec() {
        let input = vec![0.1_f32, 0.2, 0.3];
        let output = process(&input);
        // process() pads to frame_size and returns hop_size samples.
        assert!(!output.is_empty());
        // Should be finite (no NaN / inf).
        for &v in &output {
            assert!(v.is_finite(), "got non-finite: {v}");
        }
    }
}
