/** Tiny reactive Subject — minimal pub/sub used by ReceiverSession streams.
 *  SolidJS signals work great for UI state, but FFT frames and metadata events
 *  are high-frequency and push-based; we use this lightweight Subject instead
 *  of creating a SolidJS signal per frame. Components call .subscribe() in
 *  onMount and unsubscribe in onCleanup.
 */
export class Subject<T> {
  private observers = new Set<(value: T) => void>();

  subscribe(fn: (value: T) => void): () => void {
    this.observers.add(fn);
    return () => this.observers.delete(fn);
  }

  emit(value: T): void {
    // Iterate over a snapshot so unsubscribing during emit doesn't break the loop.
    const snapshot = Array.from(this.observers);
    for (const fn of snapshot) fn(value);
  }

  get subscriberCount(): number {
    return this.observers.size;
  }

  dispose(): void {
    this.observers.clear();
  }
}
