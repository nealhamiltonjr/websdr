//! OpenWebRX+ AI inference module (Rust).
//!
//! Stage 2a of the DSP+AI cascade (see ADR-002): server-side DeepFilterNet
//! noise reduction on the demodulated audio path.
//!
//! Slice-1 status: stub. This crate builds, exposes a `process` entry
//! point that no-ops, and reserves the FFI surface. Real DeepFilterNet
//! integration lands in slice-2 with model loading and GPU detection.

pub mod deepfilternet;

/// Library version string.
#[no_mangle]
pub extern "C" fn owrx_ai_version() -> *const std::os::raw::c_char {
    static VERSION: &[u8] = b"openwebrx-plus ai 0.1.0 (slice-1 stub)\0";
    VERSION.as_ptr() as *const std::os::raw::c_char
}

/// Smoke test: returns the sum of two i32 — proves the C ABI works.
#[no_mangle]
pub extern "C" fn owrx_ai_add(a: i32, b: i32) -> i32 {
    a + b
}

/// Process a frame of audio samples through DeepFilterNet.
///
/// # Arguments
/// * `samples` - interleaved f32 audio samples, frame size = 480 (RNNoise-compatible)
///
/// # Returns
/// Denoised samples, same length. In slice-1 stub, returns the input unchanged.
pub fn process(samples: &[f32]) -> Vec<f32> {
    // TODO(slice-2): real DeepFilterNet inference here
    samples.to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn smoke_add() {
        assert_eq!(owrx_ai_add(2, 3), 5);
    }

    #[test]
    fn process_passthrough_in_stub() {
        let input = vec![0.1_f32, 0.2, 0.3];
        let output = process(&input);
        assert_eq!(input, output);
    }
}
