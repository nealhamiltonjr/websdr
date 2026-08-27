// Smoke binary — verify the AI crate builds as a standalone executable.
//
// Run: cargo run --bin owrx-ai-test

fn main() {
    println!("owrx-ai 2+2 = {}", owrx_ai::owrx_ai_add(2, 2));
    let input = vec![0.1_f32, 0.2, 0.3];
    let output = owrx_ai::process(&input);
    println!("process({:?}) -> {:?}", input, output);
}
