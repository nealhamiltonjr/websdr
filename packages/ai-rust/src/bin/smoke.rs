// Smoke binary — verify the AI crate builds as a standalone executable.
//
// Run: cargo run --bin owrx-ai-test
//
// Slice-18: exercises the new Denoiser (real spectral-subtraction
// algorithm) via the Rust API (not just the C ABI smoke test in
// lib.rs::owrx_ai_add).

use owrx_ai::{Denoiser, DenoiserConfig};

fn main() {
    println!(
        "owrx-ai version: {}",
        unsafe {
            std::ffi::CStr::from_ptr(owrx_ai::owrx_ai_version())
                .to_str()
                .unwrap_or("(invalid utf8)")
        }
    );
    println!("owrx-ai 2+2 = {}", owrx_ai::owrx_ai_add(2, 2));

    let cfg = DenoiserConfig::default();
    println!(
        "Denoiser config: frame_size={}, fft_size={}, hop_size={}",
        cfg.frame_size, cfg.fft_size, cfg.hop_size
    );

    let mut d = Denoiser::new(cfg).expect("Denoiser::new");
    println!("Initial samples_processed: {}", d.samples_processed());

    // Process a 1 kHz tone at 8 kHz SR for a few frames.
    let mut total_energy_in = 0.0_f32;
    let mut total_energy_out = 0.0_f32;
    for frame in 0..5 {
        let mut samples = vec![0.0_f32; d.frame_size()];
        for i in 0..samples.len() {
            let t = (frame * d.frame_size() + i) as f32;
            samples[i] = 0.5 * (2.0 * std::f32::consts::PI * 1000.0 * t / 8000.0).sin();
            total_energy_in += samples[i] * samples[i];
        }
        d.process_frame(&mut samples).expect("process_frame");
        // The first hop_size samples of `samples` are the denoised output.
        for i in 0..d.config.hop_size.min(samples.len()) {
            total_energy_out += samples[i] * samples[i];
        }
    }
    println!("After 5 frames: samples_processed={}", d.samples_processed());
    println!("Input energy:  {:.3}", total_energy_in);
    println!("Output energy: {:.3}", total_energy_out);
    println!(
        "Energy ratio (out/in over first hop_size of each frame): {:.3}",
        total_energy_out / total_energy_in.max(1e-9)
    );
    println!("All smoke checks passed.");
}
