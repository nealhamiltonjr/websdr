# OpenWebRX+ shared-types

Cross-language wire-format contracts. The TypeScript side is consumed by the SolidJS frontend; the Python counterparts (`openwebrx_plus_types`) live in `python/openwebrx_plus_types/` and will be generated from these schemas via `datamodel-code-generator` in slice-2.

## Status (slice-1)

- [x] TypeScript shapes for `FFTFrame`, `ReceiverMetadata`, `SourceInfo`, `ReceiverMode`, `DSPMode`
- [x] Client → server control messages (`SubscribeMessage`, `SetFrequencyMessage`, etc.)
- [x] Server → client metadata events (`MetadataEvent`, `OpenEvent`, `CloseEvent`, `DecoderEvent`)
- [x] Binary FFT header constants
- [ ] Zod schemas (runtime validation) — slice-2
- [ ] Python equivalents — slice-2 (via datamodel-code-generator)

## Usage (from `apps/web`)

```ts
import type { FFTFrame, ReceiverMetadata } from '@openwebrx-plus/shared-types';
```

## Wire format (binary FFT frame)

```
| offset | size | field           | type | description                              |
|--------|------|-----------------|------|------------------------------------------|
| 0      | 4    | magic           | u32  | 0x4f465257 ("WRFO")                      |
| 4      | 4    | version         | u32  | 1                                        |
| 8      | 4    | receiverIdHash  | u32  | low 32 bits of hash(receiverId)          |
| 12     | 4    | centerFreq      | f32  | Hz                                       |
| 16     | 4    | sampleRate      | f32  | Hz                                       |
| 20     | 4    | minDb           | f32  | display range hint                       |
| 24     | 4    | maxDb           | f32  | display range hint                       |
| 28     | 4    | binCount        | u32  | length of bins array                     |
| 32     | 4    | timestampMs     | u32  | low 32 bits of performance.now()         |
| 36     | N*4  | bins            | f32  | N = binCount, power in dBFS             |
```

All integers are little-endian. The full `receiverId` (string) is NOT in the binary frame — clients should match it to the metadata they received earlier via the hash. This keeps frame size tight at high FPS.
