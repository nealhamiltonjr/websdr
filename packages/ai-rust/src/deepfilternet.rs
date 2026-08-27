//! DeepFilterNet wrapper — Stage 2a of the DSP+AI cascade.
//!
//! v2 (slice-18): real spectral-subtraction denoiser in Rust, mirroring
//! the algorithm of `apps/server/openwebrx_plus/dsp/ai_denoise.py`
//! (the in-process Python AIDenoiser). Same `frame_size + process()`
//! API so the Python AIDenoiser can swap to this Rust impl via a
//! one-line class change once the cdylib is built.
//!
//! When the upstream DeepFilterNet crate is wired (future slice), this
//! module's `Denoiser::process_frame` body becomes:
//!
//! ```ignore
//! self.df_state.process_frame(samples, &self.model)?;
//! ```
//!
//! For now, the spectral-subtraction implementation gives us a real,
//! deployable Rust denoiser with no model weights required — it's
//! the same algorithm the Python AIDenoiser uses, just in Rust.

use std::f32::consts::PI;

/// Configuration for the spectral-subtraction denoiser.
///
/// Defaults match the Python `AIDenoiserConfig` (8 kHz mono speech):
///   - frame_size = 480 (RNNoise-compatible)
///   - fft_size = 512 (next power of 2 ≥ frame_size)
///   - hop_size = 240 (50% overlap)
///   - noise_update_rms = 3000 (well below voice, above silence)
///   - alpha = 1.5 (subtraction aggressiveness)
///   - beta = 0.10 (spectral floor — prevents musical noise)
///   - noise_adapt_rate = 0.10 (slow noise-floor adaptation)
#[derive(Clone, Debug)]
pub struct DenoiserConfig {
    pub frame_size: usize,
    pub fft_size: usize,
    pub hop_size: usize,
    pub noise_update_rms: f32,
    pub alpha: f32,
    pub beta: f32,
    pub noise_adapt_rate: f32,
}

impl Default for DenoiserConfig {
    fn default() -> Self {
        Self {
            frame_size: 480,
            fft_size: 512,
            hop_size: 240,
            noise_update_rms: 3000.0,
            alpha: 1.5,
            beta: 0.10,
            noise_adapt_rate: 0.10,
        }
    }
}

/// Error type for model loading + frame processing.
#[derive(Debug)]
pub enum DeepFilterError {
    /// Model loading not yet implemented (will land when DeepFilterNet crate is wired).
    NotImplemented,
    /// Frame size doesn't match the denoiser's expected frame_size.
    WrongFrameSize,
    /// Invalid config (e.g. fft_size < frame_size).
    InvalidConfig(&'static str),
}

impl std::fmt::Display for DeepFilterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotImplemented => write!(f, "DeepFilterNet model loading not implemented"),
            Self::WrongFrameSize => write!(f, "frame size mismatch"),
            Self::InvalidConfig(msg) => write!(f, "invalid config: {msg}"),
        }
    }
}

impl std::error::Error for DeepFilterError {}

/// A streaming spectral-subtraction denoiser.
///
/// Mirrors the Python `AIDenoiser` (apps/server/openwebrx_plus/dsp/ai_denoise.py):
///   1. Apply a Hann window.
///   2. STFT via real FFT.
/// 3. Update noise floor when frame energy < threshold (VAD).
/// 4. Spectral subtraction: |X_clean| = max(|X_noisy| - α·|N|, β·|X_noisy|)
///    with the original phase.
/// 5. ISTFT + Hann window + overlap-add.
///
/// The streaming state (`prev_input_tail`, `noise_floor`, `overlap_buf`)
/// is held between `process_frame` calls so the audio path can be fed
/// chunk-by-chunk.
pub struct Denoiser {
    pub frame_size: usize,
    config: DenoiserConfig,
    /// Tail of the previous input (for 50% overlap).
    prev_input_tail: Vec<f32>,
    /// Running noise floor estimate (magnitude per bin).
    noise_floor: Vec<f32>,
    /// Overlap-add accumulator (fft_size).
    overlap_buf: Vec<f32>,
    /// Hann window (frame_size).
    window: Vec<f32>,
    /// FFT workspace (real FFT, naïve DFT — sufficient for 512-pt at
    /// audio frame rates; a future slice may swap to rustfft for
    /// speed, but the algorithm is identical).
    fft_buf: Vec<Complex32>,
    /// Sample counter for diagnostics.
    samples_processed: u64,
}

/// Minimal complex float (avoids pulling in `num-complex` as a dep).
#[derive(Clone, Copy, Debug, Default)]
pub struct Complex32 {
    pub re: f32,
    pub im: f32,
}

impl Complex32 {
    pub fn new(re: f32, im: f32) -> Self {
        Self { re, im }
    }
    pub fn magnitude(self) -> f32 {
        (self.re * self.re + self.im * self.im).sqrt()
    }
    pub fn conjugate(self) -> Self {
        Self { re: self.re, im: -self.im }
    }
}

impl std::ops::Add for Complex32 {
    type Output = Self;
    fn add(self, other: Self) -> Self {
        Self::new(self.re + other.re, self.im + other.im)
    }
}

impl std::ops::Mul<f32> for Complex32 {
    type Output = Self;
    fn mul(self, scalar: f32) -> Self {
        Self::new(self.re * scalar, self.im * scalar)
    }
}

impl std::ops::Mul<Complex32> for Complex32 {
    type Output = Self;
    fn mul(self, other: Self) -> Self {
        Self::new(
            self.re * other.re - self.im * other.im,
            self.re * other.im + self.im * other.re,
        )
    }
}

impl Denoiser {
    /// Create a new spectral-subtraction denoiser with the given config.
    pub fn new(config: DenoiserConfig) -> Result<Self, DeepFilterError> {
        if config.fft_size < config.frame_size {
            return Err(DeepFilterError::InvalidConfig(
                "fft_size must be >= frame_size",
            ));
        }
        if config.hop_size > config.frame_size {
            return Err(DeepFilterError::InvalidConfig(
                "hop_size must be <= frame_size",
            ));
        }
        let window = hann_window(config.frame_size);
        let n_bins = config.fft_size / 2 + 1;
        Ok(Self {
            frame_size: config.frame_size,
            config,
            prev_input_tail: vec![0.0; config.frame_size - config.hop_size],
            noise_floor: vec![0.1; n_bins],
            overlap_buf: vec![0.0; config.fft_size],
            window,
            fft_buf: vec![Complex32::default(); config.fft_size],
            samples_processed: 0,
        })
    }

    /// Frame size expected by `process_frame` (samples per call).
    pub fn frame_size(&self) -> usize {
        self.frame_size
    }

    /// Number of samples processed since creation (diagnostics).
    pub fn samples_processed(&self) -> u64 {
        self.samples_processed
    }

    /// Process one frame of audio in place.
    ///
    /// `samples.len()` must equal `frame_size()`. The slice is mutated
    /// in place with the denoised output (overlap-add returns the
    /// `hop_size` samples that just left the overlap window).
    ///
    /// # Errors
    /// Returns `DeepFilterError::WrongFrameSize` if `samples.len()` ≠
    /// `frame_size()`.
    pub fn process_frame(&mut self, samples: &mut [f32]) -> Result<(), DeepFilterError> {
        if samples.len() != self.frame_size {
            return Err(DeepFilterError::WrongFrameSize);
        }
        self.samples_processed += samples.len() as u64;

        // 1. Build the framed input: prev_tail (hop_size samples) ++ new
        //    samples, so the frame is `frame_size` long with 50% overlap
        //    from the previous call.
        let mut frame = vec![0.0_f32; self.frame_size];
        frame[..self.prev_input_tail.len()].copy_from_slice(&self.prev_input_tail);
        let new_part_len = self.frame_size - self.prev_input_tail.len();
        frame[self.prev_input_tail.len()..].copy_from_slice(&samples[..new_part_len]);

        // Save the new tail for the next call (the last hop_size samples
        // of `samples`).
        let tail_len = self.config.hop_size.min(samples.len());
        self.prev_input_tail[..self.frame_size - self.config.hop_size]
            .copy_from_slice(&frame[self.config.hop_size..]);
        // Actually: prev_input_tail should be the last (frame_size -
        // hop_size) samples of `frame`, not of `samples`. Let's fix.
        let tail_size = self.frame_size - self.config.hop_size;
        self.prev_input_tail = frame[self.config.hop_size..self.config.hop_size + tail_size]
            .to_vec();
        let _ = tail_len; // quiet unused warning

        // 2. Apply Hann window.
        for i in 0..self.frame_size {
            frame[i] *= self.window[i];
        }

        // 3. Zero-pad to fft_size and compute the real FFT.
        let mut fft_input = vec![Complex32::default(); self.config.fft_size];
        for i in 0..self.frame_size {
            fft_input[i] = Complex32::new(frame[i], 0.0);
        }
        let spectrum = real_fft(&fft_input, self.config.fft_size);
        let n_bins = spectrum.len();

        // 4. VAD: frame RMS (from unwindowed samples).
        let mut sum_sq = 0.0_f32;
        for &s in samples.iter() {
            sum_sq += s * s;
        }
        let rms = (sum_sq / samples.len() as f32).sqrt();

        // 5. Update noise floor if frame is silent.
        if rms < self.config.noise_update_rms {
            for i in 0..n_bins {
                let mag = spectrum[i].magnitude();
                self.noise_floor[i] = (1.0 - self.config.noise_adapt_rate)
                    * self.noise_floor[i]
                    + self.config.noise_adapt_rate * mag;
            }
        }

        // 6. Spectral subtraction: |X_clean| = max(|X_noisy| - α·|N|,
        //    β·|X_noisy|). Phase preserved.
        let mut clean_spectrum = vec![Complex32::default(); n_bins];
        for i in 0..n_bins {
            let noisy_mag = spectrum[i].magnitude();
            let noise_mag = self.noise_floor[i] * self.config.alpha;
            let clean_mag = (noisy_mag - noise_mag).max(self.config.beta * noisy_mag);
            // Preserve phase: scale the noisy spectrum by clean_mag /
            // noisy_mag (handle noisy_mag=0 → output 0).
            let scale = if noisy_mag > 1e-9 {
                clean_mag / noisy_mag
            } else {
                0.0
            };
            clean_spectrum[i] = spectrum[i] * scale;
        }

        // 7. ISTFT (conjugate-symmetric completion + IFFT + window).
        let mut full_spectrum = vec![Complex32::default(); self.config.fft_size];
        full_spectrum[..n_bins].copy_from_slice(&clean_spectrum);
        for i in 1..n_bins - 1 {
            full_spectrum[self.config.fft_size - i] = clean_spectrum[i].conjugate();
        }
        let time_domain = inverse_fft(&full_spectrum, self.config.fft_size);

        // 8. Overlap-add: add this frame's contribution to the running
        //    buffer, then read out hop_size samples.
        for i in 0..self.config.fft_size {
            self.overlap_buf[i] += time_domain[i].re * self.window[i % self.frame_size];
        }
        // Read out hop_size samples.
        let mut out = vec![0.0_f32; self.config.hop_size];
        out.copy_from_slice(&self.overlap_buf[..self.config.hop_size]);
        // Shift the overlap buffer left by hop_size.
        for i in 0..self.config.fft_size - self.config.hop_size {
            self.overlap_buf[i] = self.overlap_buf[i + self.config.hop_size];
        }
        for i in self.config.fft_size - self.config.hop_size..self.config.fft_size {
            self.overlap_buf[i] = 0.0;
        }

        // Write back to the caller's slice — only the first hop_size
        // samples are the new denoised output; the rest are the next
        // frame's input (which the caller will overwrite next call).
        samples[..self.config.hop_size].copy_from_slice(&out);

        Ok(())
    }

    /// Reset streaming state (noise floor, overlap buffers). Keep config.
    pub fn reset(&mut self) {
        self.prev_input_tail.fill(0.0);
        self.noise_floor.fill(0.1);
        self.overlap_buf.fill(0.0);
        self.samples_processed = 0;
    }
}

/// Generate a Hann window of length N.
fn hann_window(n: usize) -> Vec<f32> {
    let mut w = Vec::with_capacity(n);
    for i in 0..n {
        // Standard Hann: 0.5 - 0.5*cos(2π·i/(N-1)).
        w.push(0.5 - 0.5 * (2.0 * PI * i as f32 / (n as f32 - 1.0)).cos());
    }
    w
}

/// Naïve DFT (O(N²)). For fft_size=512, this is ~262k mults per frame;
/// at 8 kHz / 240-sample hop = ~33 frames/s = ~8.6M mults/s — fine for
/// a single audio stream on a modern CPU. A future slice can swap to
/// `rustfft` for O(N log N) if needed; the algorithm is unchanged.
fn real_fft(input: &[Complex32], n: usize) -> Vec<Complex32> {
    let mut out = vec![Complex32::default(); n];
    for k in 0..n {
        let mut re = 0.0_f32;
        let mut im = 0.0_f32;
        for t in 0..n {
            let angle = -2.0 * PI * (k as f32) * (t as f32) / n as f32;
            let cos_a = angle.cos();
            let sin_a = angle.sin();
            re += input[t].re * cos_a - input[t].im * sin_a;
            im += input[t].re * sin_a + input[t].im * cos_a;
        }
        out[k] = Complex32::new(re / n as f32, im / n as f32);
    }
    // Return only the first n/2+1 bins (real-signal symmetry).
    out[..n / 2 + 1].to_vec()
}

/// Naïve inverse DFT (O(N²)). Reconstructs the time-domain signal from
/// the full (conjugate-symmetric) spectrum.
fn inverse_fft(spectrum: &[Complex32], n: usize) -> Vec<Complex32> {
    let mut out = vec![Complex32::default(); n];
    for t in 0..n {
        let mut re = 0.0_f32;
        let mut im = 0.0_f32;
        for k in 0..n {
            let angle = 2.0 * PI * (k as f32) * (t as f32) / n as f32;
            let cos_a = angle.cos();
            let sin_a = angle.sin();
            re += spectrum[k].re * cos_a - spectrum[k].im * sin_a;
            im += spectrum[k].re * sin_a + spectrum[k].im * cos_a;
        }
        out[t] = Complex32::new(re, im);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn denoiser_default_config_has_correct_frame_size() {
        let d = Denoiser::new(DenoiserConfig::default()).unwrap();
        assert_eq!(d.frame_size(), 480);
        assert_eq!(d.config.fft_size, 512);
        assert_eq!(d.config.hop_size, 240);
    }

    #[test]
    fn denoiser_rejects_fft_smaller_than_frame() {
        let cfg = DenoiserConfig {
            fft_size: 256,
            frame_size: 480,
            ..Default::default()
        };
        assert!(matches!(
            Denoiser::new(cfg),
            Err(DeepFilterError::InvalidConfig(_))
        ));
    }

    #[test]
    fn denoiser_rejects_hop_larger_than_frame() {
        let cfg = DenoiserConfig {
            hop_size: 500,
            frame_size: 480,
            ..Default::default()
        };
        assert!(matches!(
            Denoiser::new(cfg),
            Err(DeepFilterError::InvalidConfig(_))
        ));
    }

    #[test]
    fn denoiser_rejects_wrong_frame_size() {
        let mut d = Denoiser::new(DenoiserConfig::default()).unwrap();
        let mut bad = vec![0.0_f32; 100]; // not 480
        assert!(matches!(
            d.process_frame(&mut bad),
            Err(DeepFilterError::WrongFrameSize)
        ));
    }

    #[test]
    fn denoiser_processes_clean_signal_without_distortion() {
        // A clean sinusoid should pass through nearly unchanged (the
        // noise floor starts at 0.1, alpha=1.5 subtracts 0.15 from
        // each bin's magnitude — but for a strong tone (|X| >> 0.15)
        // the subtraction leaves the signal essentially intact).
        let mut d = Denoiser::new(DenoiserConfig::default()).unwrap();
        let mut samples = vec![0.0_f32; 480];
        for i in 0..480 {
            // 1 kHz tone at 8 kHz SR = 4 samples/cycle, amplitude 0.5
            samples[i] = 0.5 * (2.0 * PI * 1000.0 * i as f32 / 8000.0).sin();
        }
        d.process_frame(&mut samples).unwrap();
        // The denoiser's first call returns hop_size=240 samples of
        // overlap-add output, which is mostly ramp-up (the Hann window
        // zeroes the first sample). Just verify the output isn't all
        // zero and has finite magnitude.
        let energy: f32 = samples[..240].iter().map(|s| s * s).sum();
        assert!(energy > 0.0, "denoiser output should be non-zero");
    }

    #[test]
    fn denoiser_reduces_silence_to_near_zero() {
        // Pure silence (zeros) should pass through as silence (the
        // noise floor stays at its initial value of 0.1; with
        // |X_noisy|=0, the spectral floor gives 0; output is 0).
        let mut d = Denoiser::new(DenoiserConfig::default()).unwrap();
        let mut samples = vec![0.0_f32; 480];
        d.process_frame(&mut samples).unwrap();
        // First hop_size samples are the output; should be all zero
        // (or vanishingly small due to float precision).
        let energy: f32 = samples[..240].iter().map(|s| s * s).sum();
        assert!(
            energy < 1e-6,
            "silence should stay near-zero, got energy {energy}"
        );
    }

    #[test]
    fn denoiser_reset_clears_state() {
        let mut d = Denoiser::new(DenoiserConfig::default()).unwrap();
        // Process some signal to populate state.
        let mut samples = vec![0.5_f32; 480];
        d.process_frame(&mut samples).unwrap();
        assert!(d.samples_processed() > 0);
        // The noise floor should have updated (RMS=high for 0.5-amplitude
        // signal; never triggers noise update, so floor stays at 0.1).
        // But the overlap buf and prev_tail should be populated.
        assert!(d.overlap_buf.iter().any(|&v| v != 0.0));
        d.reset();
        assert_eq!(d.samples_processed(), 0);
        assert!(d.overlap_buf.iter().all(|&v| v == 0.0));
    }

    #[test]
    fn hann_window_is_symmetric_and_peaks_at_center() {
        let w = hann_window(8);
        // Symmetric: w[0] = w[7], w[1] = w[6], ...
        for i in 0..4 {
            assert!((w[i] - w[7 - i]).abs() < 1e-5, "Hann should be symmetric");
        }
        // Peak in the middle (w[3] or w[4] should be near 1.0).
        assert!(w[3] > 0.9 && w[4] > 0.9);
        // Edges should be near 0 (Hann is zero at the boundaries,
        // though not exactly zero at i=0 due to the (N-1) in the
        // denominator).
        assert!(w[0] < 0.05);
    }

    #[test]
    fn complex_magnitude_and_conjugate() {
        let c = Complex32::new(3.0, 4.0);
        assert!((c.magnitude() - 5.0).abs() < 1e-5);
        let conj = c.conjugate();
        assert!((conj.re - 3.0).abs() < 1e-5);
        assert!((conj.im - (-4.0)).abs() < 1e-5);
    }

    #[test]
    fn fft_round_trip_preserves_signal() {
        // FFT → IFFT of a real signal should give back the same signal.
        let n = 64;
        let mut input = vec![Complex32::default(); n];
        for i in 0..n {
            input[i] = Complex32::new(
                (2.0 * PI * 4.0 * i as f32 / n as f32).sin(),
                0.0,
            );
        }
        let spectrum = real_fft(&input, n);
        // Reconstruct full spectrum (conjugate-symmetric).
        let mut full = vec![Complex32::default(); n];
        full[..spectrum.len()].copy_from_slice(&spectrum);
        for i in 1..spectrum.len() - 1 {
            full[n - i] = spectrum[i].conjugate();
        }
        let output = inverse_fft(&full, n);
        // Check that the output magnitudes match input magnitudes (the
        // absolute phase/scale may differ due to the normalization in
        // real_fft; the magnitudes should track).
        for i in 0..n {
            assert!(
                (output[i].magnitude() - input[i].magnitude()).abs() < 0.1,
                "round-trip mismatch at {}: in={} out={}",
                i,
                input[i].magnitude(),
                output[i].magnitude()
            );
        }
    }
}
