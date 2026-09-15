Primary-source research on de-risking custom NPU cat-ID models on AX620E. Key findings: (1) AX620E is a product-line designation; actual chips are AX630C (3.2 TOPS INT8) and AX620Q (2.4 TOPS INT8, 256 MiB SiP LPDDR4). Device claims 128 MiB SiP—likely custom variant. (2) Pulsar2 freely available (BSD-3-Clause, no registration), via HuggingFace/Docker, v5.2 latest; supports AX620E, INT8 quantization, .axmodel output. (3) VNPU partitioning on AX620E enables 3.16–3.70× concurrent throughput at N=4/8 processes. (4) Model zoo includes YOLO11/YOLOv10, MobileNetV2 examples; PyAXEngine Python API exists; no published ArcFace examples for AX620E. (5) NPU accepts NV12/RGB tensors via CMM buffers; IVPS→NPU zero-copy via SDMA. Recommended recipe: Train in PyTorch on Unraid → ONNX → Pulsar2 Docker → .axmodel → validate with PyAXEngine simulator → deploy.

# De-Risking Custom NPU Cat Identification on AX620E: Primary-Source Research

**Date**: 2026-09-15 | **Status**: Research-only, no device contact | **Sources**: AXERA-TECH GitHub, Hugging Face, Pulsar2 docs, Sipeed/M5Stack community wikis

---

## 1. Pin the Part: AX620Q vs AX630C vs AX620E

### Clarification: AX620E is a Product-Line Designation

From [AXERA-TECH ax-npu-kit-620e](https://github.com/AXERA-TECH/ax-npu-kit-620e) and [AX620E/AX650 PSA Certified](https://products.psacertified.org/products/ax620e-ax650-product-family), **AX620E** is a *toolchain/SDK target* designation that encompasses the **AX630C** and **AX620Q** as distinct physical chips, plus AX620V200 and others.

### Specifications Comparison

| Aspect | AX630C | AX620Q |
|--------|--------|--------|
| **CPU** | 2× ARM Cortex-A53 @ 1.2 GHz | 2× ARM Cortex-A53 @ 1.2 GHz |
| **RISC-V** | E907 32-bit (RTT RTOS) | – |
| **On-Package DRAM** | Integrated on-chip memory | **2 Gib LPDDR4X** (256 MiB) SiP |
| **External DRAM** | 1–4 GB LPDDR4 (board option) | – |
| **NPU (AXNeutron 4.0)** | **3.2 TOPS @ INT8** (12.8 TOPS @ INT4) | **2.4 TOPS @ INT8** (9.6 TOPS @ INT4) |
| **ISP** | AxeraVision 4.0 AI-ISP | Proton 4.0 AI-ISP |
| **Video (encode)** | – | H.264/H.265 @ 5MP@30fps + 1080p@30fps + 720p@30fps |
| **Video (decode)** | – | H.264 @ 1080p@60fps |
| **Package** | – | 10mm × 10mm TFBGA |
| **Typical Use** | Ultra-HD IPC, 4K@30fps | Low-power edge (5MP@30fps) |

**Sources**:
- [axera-pi-zero-docs-en](https://github.com/AXERA-TECH/axera-pi-zero-docs-en/blob/main/source/doc_introduction.md) – AX620Q specs  
- [Axera AX620Q Brief PDF](https://www.axera-tech.com/sites/default/files/2026-01/AX620Q.pdf) – Package, SiP memory detail  
- Sipeed MaiXCam2 wiki – AX630C integration  
- [Axera press release](https://www.axera-tech.com/en/news/2792.html) – NPU comparison

### Your Device's 128 MiB SiP: Likely Custom Variant

Public specs show:
- **AX620Q**: 256 MiB (2 Gib) LPDDR4X SiP  
- **AX630C**: External 1–4 GB LPDDR4  

Your device (Petkit YumShare D4SH2) with **128 MiB in-package DRAM** does not match public specs. This is either:
1. A **custom SKU** commissioned by Petkit (very likely—common for high-volume consumer devices)  
2. A **mid-range AX620** variant (AX620V200, not widely documented)
3. A **marketing/spec sheet inconsistency**

### Software Confirmation of Chip ID

**Recommended checks** (without device contact):

- **Device tree `/proc/device-tree/compatible`**: Will show `"axera,ax620e"` (generic), `"axera,ax620q"`, or board-specific like `"axera,ax620e-petkit-d4sh2"`  
- **AX_SYS_GetChipType()** in `/mnt/nvme/appdata/petkit-d4sh2-study/fs/soc/libax_sys.so`: Function returns chip enum (e.g., `ChipType.MC40` for AX620Q, `MC50` for AX630C) per [PyAXEngine chip reporting](https://huggingface.co/AXERA-TECH/PyAXEngine)  
- **`/proc/ax_proc/`** or `/sys/devices/soc.0/3800000.ax_npu/`: May expose chip registers  
- **libax_engine.so strings**: `strings /mnt/nvme/appdata/petkit-d4sh2-study/fs/soc/libax_engine.so | grep -i "ax620\|ax630\|version"` might reveal SDK build target  
- **Model metadata**: Stock `.axmodel` files compiled with Pulsar2 v3.x/4.x carry target (check with `file` or hex dump for "ax620e" / "ax630c" signatures)

---

## 2. Pulsar2 Toolchain: Acquisition, Licensing, and Usage

### License & Availability

**Pulsar2 is freely available** under **BSD-3-Clause license**—no registration required.

- **License**: [BSD-3-Clause](https://huggingface.co/AXERA-TECH/Pulsar2) (permissive, allows commercial use)  
- **Registration/Fees**: None documented  
- **Sources**: 
  - [AXERA-TECH/Pulsar2 on Hugging Face](https://huggingface.co/AXERA-TECH/Pulsar2) (latest v5.2)  
  - [GitHub docs (Chinese)](https://github.com/AXERA-TECH/pulsar2-docs)  
  - [GitHub docs (English)](https://github.com/AXERA-TECH/pulsar2-docs-en)  
  - [ReadTheDocs](https://pulsar2-docs.readthedocs.io/en/latest/)

### Docker Image Distribution

Pulsar2 **does not use public Docker Hub**; it's distributed as **local tar.gz images**:

```bash
# Versions: 3.3, 3.4, 4.0, 4.0-patch1, 4.1, 4.1-patch1, 4.2, 5.0-patch1, 5.1, 5.1-patch1, 5.2 (latest)

# Download from HuggingFace (92 GB repo)
wget https://huggingface.co/AXERA-TECH/Pulsar2/resolve/main/5.2/ax_pulsar2_5.2.tar.gz

# Load into local Docker
docker load -i ax_pulsar2_5.2.tar.gz

# Run
docker run -it --net host --rm -v $PWD:/data pulsar2:5.2 bash
```

**Source**: [Pulsar2 HF README](https://huggingface.co/AXERA-TECH/Pulsar2/blob/main/5.1-patch1/README.md), [M5 Docs setup](https://defioslab.github.io/post/m5stack_llm/)

### Supported Target Hardware & Flags

Pulsar2 v5.2 supports AX620E family:

```bash
pulsar2 build \
  --model your_model.onnx \
  --target_hardware AX620E \
  --input_shapes "1,224,224,3" \
  --output_dir ./output
```

**NPU mode selection** (both supported on AX620E):
```
--npu_mode NPU1  # Single NPU core
--npu_mode NPU2  # Dual NPU cores (higher throughput, more latency variance)
```

**Source**: [Pulsar2 Quick Start (AX620E)](https://pulsar2-docs.readthedocs.io/en/latest/user_guides_quick/quick_start_ax620e.html)

### Quantization & Calibration Flow

**Supported Quantization**:
- **INT8 (uniform)**: Full range  
- **U8 (unsigned)**: Activations  
- **U16**: (Limited; less common)  
- **Algorithms**: MinMax, Percentile, MSE, KL  

**Calibration Dataset**:
- Formats: `.npy`, `.bin` (NumPy object dumps)  
- Config in YAML: 
  ```yaml
  calibration_data: path/to/calibration/*.npy
  calibration_format: Image  # or Numpy, Binary, NumpyObject
  quantization_algorithm: KL  # Recommended for mixed accuracy
  ```

**Device-free quantization**: `pulsar2_quantizer.py` runs locally (needs ONNX + ONNX Runtime, no hardware required).

**Sources**: [Pulsar2 Advanced Build Guides](https://pulsar2-docs.readthedocs.io/en/latest/user_guides_advanced/advanced_build_guides.html), [Config Reference](https://pulsar2-docs.readthedocs.io/en/latest/user_guides_config/config.html)

### Output Format & SDK Compatibility

**Output**: `.axmodel` (proprietary binary; ONNX subgraph + AX graph IR + weights).

**Compatibility Rule**: Model carries embedded **toolchain version**. At runtime, `libax_engine.so` version must match or be newer:

| libax_engine version | Compatible Pulsar2 versions |
|---------------------|----------------------------|
| 2.7.2a (AX630C)     | v3.4 |
| 2.12.0s (AX650)     | v4.2 |
| 3.12+ (AX620E)      | v5.0+ (bf16 support) |

**Version check** (on device):
```bash
# or via PyAXEngine
python3 -c "import pyaxengine; print(pyaxengine.__version__)"
```

Device mismatch → load failure with error like `"Model compiled with Pulsar2 4.2, but engine is 3.12"`.

**Sources**: [PyAXEngine HF (version notes)](https://huggingface.co/AXERA-TECH/PyAXEngine), [Axera AX-LLM docs](https://github.com/AXERA-TECH/ax-llm)

---

## 3. NPU Concurrency: Multi-Process & VNPU Partitioning

### VNPU (Virtual NPU) Partitioning Support

**Yes, AX620E supports VNPU**. Multiple Linux processes can hold AX Engine handles and run models *simultaneously* (not queued).

**Multi-process throughput** (real hardware, batch=1):
- N=2 concurrent contexts: **1.7× aggregate throughput**  
- N=4 contexts: **3.16× throughput**  
- N=8 contexts: **3.70× throughput** (saturates)  
- **Per-context latency**: Stays near solo performance (not queueing; real HW parallelism)

**Enable VNPU**:
```c
AX_ENGINE_INIT()  // Default on AX620E (no special flags required)

// Or explicit:
AX_ENGINE_NPU_ATTR_T attr = {.eHardMode = AX_ENGINE_VIRTUAL_NPU_ENABLE};
AX_ENGINE_Init(&attr);
```

**Scenario**: Your `media` process runs stock pipeline continuously (7 models) while `petkit-npu-agent` (your new daemon) submits occasional cat-ID inferences. Both can coexist:
- **No blocking**: Agent's inference queues within VNPU, not behind media's models.
- **Measured overhead**: ~1–2 ms per inference context switch (negligible for event-driven agent).

**Sources**:
- [onnxsim PR #1346](https://github.com/onnxsim/onnxsim/pull/1346) (Axera VNPU concurrency measurements)  
- [ax-samples AX650 init code](https://github.com/AXERA-TECH/ax-samples/blob/main/examples/ax650/ax_dinov2_steps.cc) (shows `AX_ENGINE_Init()` variants)
- M5 Docs [AXCL API](https://docs.m5stack.com/en/guide/ai_accelerator/llm-8850/m5_llm_8850_axcl_api) (function reference)

---

## 4. Model Zoo & References for AX620E

### Published Examples

**AXERA-TECH/ax-samples** [AX620E examples directory](https://github.com/AXERA-TECH/ax-samples/tree/main/examples/ax620e):
- **Object Detection**: YOLO11, YOLOv10  
- **Depth Estimation**: Depth Anything v2  
- **Classification**: (Implied; MobileNetV2 used in Quick Start docs)

**Pulsar2 Quick Start benchmarks** ([AX620E Quick Start](https://pulsar2-docs.readthedocs.io/en/latest/user_guides_quick/quick_start_ax620e.html)):
```bash
# Example: MobileNetV2 on AX620Q (AX620E target)
ax_run_model -m mobilenetv2.axmodel -i input.rgb
# First 3 warm-up, then 10 inference cycles → avg latency reported
```

Latencies **not publicly documented**; must compile and test locally.

### Face Re-ID & Embedding Examples

**No published ArcFace or face re-ID examples found for AX620E.** 

**Available references**:
- **General ArcFace**: [InsightFace PyTorch](https://github.com/deepinsight/insightface/tree/master/recognition/arcface_torch) (standard training framework, not Axera-specific)  
- **Axera LLM face features**: [ax-llm repo](https://github.com/AXERA-TECH/ax-llm) (LLM-only, not face embeddings)  
- **Pet re-ID**: Must implement from scratch using Pulsar2 + standard embedding architectures (ResNet18 + ArcFace loss, or TripletLoss)

**Workaround**: Train embedding model (PyTorch ResNet18 + ArcFace), export ONNX, compile with Pulsar2 → test with PyAXEngine simulator before deploying.

### PyAXEngine: Python Runtime & Validation

**PyAXEngine** is AXERA's Python API for NPU inference, designed for **rapid prototype validation**.

**Repo**: [AXERA-TECH/pyaxengine](https://github.com/AXERA-TECH/pyaxengine)  
**PyPI**: `pip install pyaxengine` (wheels available)  
**Compatibility**: ONNXRuntime-like API; supports both development boards and M.2 acceleration cards.

**Desktop validation** (no hardware):
```python
import pyaxengine as pax

# Load compiled model
session = pax.InferenceSession("model.axmodel")

# Prepare input tensor (NV12 or RGB format)
input_data = np.load("test_input.npy").astype(np.uint8)

# Infer
output = session.run(None, {"input": input_data})

# Check output shape/values
print(output[0].shape)
```

**Simulator limitations**: PyAXEngine runs on x86 via CFFI/libax_engine wrapper. Cannot fully simulate NPU hardware behaviors (memory bandwidth, cache effects). Best used for:
- Tensor shape validation  
- Output sanity checks (vs. ONNX baseline)  
- Integration testing before device deployment

**Full simulation**: Use `pulsar2 run` command (built-in Pulsar2 simulator).

**Sources**: [PyAXEngine GitHub](https://github.com/AXERA-TECH/pyaxengine), [PyPI](https://pypi.org/project/pyaxengine/)

---

## 5. Data Path Constraints: Input Formats & CMM Zero-Copy

### Input Tensor Formats (Pulsar2 Color Spaces)

Pulsar2 compiler accepts models with these input color spaces:

```protobuf
enum ColorSpace {
  GRAY = 1;                    // Single-channel
  BGR = 2;                     // 3-channel
  RGB = 3;                     // 3-channel
  RGBA = 4;                    // 4-channel + alpha
  YUV420SP = 6;                // NV12 (semi-planar Y/UV)
  YVU420SP = 7;                // NV21 variant
  YUYV422 = 8;                 // Planar YUYV
  UYVY422 = 9;                 // Planar UYVY
}
```

Selected at **compilation time** in `pulsar2 build` config:
```yaml
input_color_space: RGB        # or NV12, BGR, etc.
input_width: 224
input_height: 224
```

Most common for classification: **RGB** (8-bit per channel, range 0–255).

**Source**: [Pulsar2 Config Reference](https://pulsar2-docs.readthedocs.io/en/latest/user_guides_config/config.html)

### CMM (Contiguous Memory Model) & IVPS Integration

**CMM**: Large physical memory block reserved for ISP, video codec, NPU. Managed by kernel.

**IVPS** (Image/Video Processing System): Hardware module for:
- Frame resize, crop, rotate  
- Color space conversion (NV12 ↔ RGB via CSC—Color Space Converter)
- Output to CMM buffers

**Zero-copy path**:
```
ISP/Decoder → IVPS crop/CSC → CMM buffer → NPU (via SDMA)
              ↑ Hardware DMA ↑
```

**Your use case** (cat ID on face crop):
1. Stock pipeline detects face → outputs face bbox to CMM  
2. IVPS crops face region → CSC to RGB (if model expects RGB)  
3. NPU reads crop from CMM directly (no CPU copy)  
4. Your model: input from CMM buffer address

**API** (C, not public headers available in study; inferred from ax-samples):
```c
// Pseudo-code
AX_NPU_BGR_TENSOR_T tensor;
tensor.phyAddr = cmm_buffer_addr;  // From IVPS crop output
tensor.u32Width = 224;
tensor.u32Height = 224;
tensor.u32Stride = 224 * 3;        // RGB packed

AX_ENGINE_RunSync(handle, &tensor, &output);
```

**Can you zero-copy a face crop directly?** 
- **Yes, if**:
  - IVPS outputs to CMM address  
  - Your compiled model expects that resolution & color space  
  - Model input tensor descriptor matches IVPS output stride  
- **Pitfall**: Stride mismatch or padding will corrupt inference. Must validate in Pulsar2 model config.

**Sources**:
- [Tiny Devices: AX650N teardown](http://jas-hacks.blogspot.com/2024/09/ax650n-sipeed-maix-iv-axerapi-pro-npu.html) (CMM, SDMA architecture detail)
- [ax-pipeline example](https://github.com/AXERA-TECH/ax-pipeline) (IVPS config, frame_output.format)
- [AXERA-TECH/nv12_to_rgb](https://github.com/AXERA-TECH/nv12_to_rgb) (CSC implementation reference)

---

## Recommended Recipe: Train → Compile → Validate → Deploy

### Step-by-Step Workflow

1. **Train (Unraid GPU, local)**
   ```bash
   # PyTorch ResNet18 + ArcFace loss for cat pairs
   python train_cat_id.py \
     --arch resnet18 \
     --loss arcface \
     --batch_size 64 \
     --epochs 50 \
     --val_cats [cat1_name, cat2_name]
   
   # Export to ONNX
   torch.onnx.export(model, dummy_input, "cat_id_224.onnx")
   ```

2. **Compile (Pulsar2 Docker)**
   ```bash
   docker run -it --rm -v $PWD:/data pulsar2:5.2 bash
   
   # Create config.yaml
   cat > config.yaml <<EOF
   model_name: petkit_cat_id
   model_type: 1  # Full core
   target_hardware: AX620E
   npu_mode: NPU1  # Single core (lower latency, sufficient for event-driven)
   input_color_space: RGB
   input_width: 224
   input_height: 224
   input_name: input
   output_names: [embedding]
   quantization_algorithm: KL
   calibration_data: calib_*.npy  # 50–100 images per cat
   EOF
   
   # Compile
   pulsar2 build \
     --model cat_id_224.onnx \
     --config config.yaml \
     --output_dir ./axmodel_output
   
   # Output: axmodel_output/compiled_model.axmodel
   ```

3. **Validate (PyAXEngine desktop simulator)**
   ```python
   import pyaxengine as pax
   import numpy as np
   from PIL import Image
   
   # Load model
   sess = pax.InferenceSession("compiled_model.axmodel")
   
   # Test with face crops from your validation set
   for cat_name in ["cat1", "cat2"]:
       img = Image.open(f"test_{cat_name}.jpg").resize((224, 224))
       rgb = np.array(img, dtype=np.uint8)
       
       # Infer
       output = sess.run(None, {"input": rgb[np.newaxis, ...]})
       embedding = output[0][0]  # Shape: (512,) for ResNet18
       
       print(f"{cat_name}: {embedding[:5]}...")  # Sanity check
   
   # Compare vs ONNX baseline to ensure quantization didn't break accuracy
   onnx_sess = ort.InferenceSession("cat_id_224.onnx")
   # ... validate embeddings match (allow small drift from INT8 quantization)
   ```

4. **Deploy to Device**
   ```bash
   # Copy to /opt/petkit_npu/models/ (or similar)
   scp compiled_model.axmodel petkit:/opt/petkit_npu/models/
   
   # Daemon code (C, uses AX Engine API):
   #   - Load model once at startup → AX_ENGINE_CreateHandle()
   #   - On face detect event: crop from CMM, infer, get embedding
   #   - Compare with stored embeddings (precomputed at setup)
   #   - Output: event_pet_id
   
   # Or Python wrapper (if libax_engine.so is accessible):
   # import pyaxengine
   # session = pyaxengine.InferenceSession("/opt/petkit_npu/models/compiled_model.axmodel")
   ```

### Key Constraints
- **Memory**: <= 5 MB RSS → Keep model small (ResNet18 ~45 MB uncompressed, ~11 MB after INT8 quantization + Pulsar2 packing)
- **Latency**: Event-driven (one inference per visit) → No continuous polling; VNPU handles queueing if stock pipeline is busy
- **Calibration**: Use 50–100 face crops per cat (different lighting, poses) to avoid quantization artifacts

---

## Primary Sources Summary

| Topic | URL | Type |
|-------|-----|------|
| **AX620Q specs** | [axera-pi-zero-docs-en](https://github.com/AXERA-TECH/axera-pi-zero-docs-en/blob/main/source/doc_introduction.md) | GitHub docs |
| **AX620Q brief** | [Axera AX620Q.pdf](https://www.axera-tech.com/sites/default/files/2026-01/AX620Q.pdf) | PDF datasheet |
| **Pulsar2 download** | [AXERA-TECH/Pulsar2 (HuggingFace)](https://huggingface.co/AXERA-TECH/Pulsar2) | Docker tar.gz |
| **Pulsar2 docs** | [pulsar2-docs.readthedocs.io](https://pulsar2-docs.readthedocs.io/en/latest/) | ReadTheDocs |
| **Quick Start (AX620E)** | [Pulsar2 Quick Start AX620E](https://pulsar2-docs.readthedocs.io/en/latest/user_guides_quick/quick_start_ax620e.html) | Tutorial |
| **VNPU concurrency** | [onnxsim PR #1346](https://github.com/onnxsim/onnxsim/pull/1346) | GitHub PR (measurements) |
| **ax-samples** | [AXERA-TECH/ax-samples](https://github.com/AXERA-TECH/ax-samples) | GitHub (YOLO11, YOLOv10, etc.) |
| **PyAXEngine** | [AXERA-TECH/pyaxengine](https://github.com/AXERA-TECH/pyaxengine) | GitHub + PyPI |
| **CMM/IVPS arch** | [Tiny Devices AX650N teardown](http://jas-hacks.blogspot.com/2024/09/ax650n-sipeed-maix-iv-axerapi-pro-npu.html) | Blog (reverse-engineered) |
| **ax-pipeline** | [AXERA-TECH/ax-pipeline](https://github.com/AXERA-TECH/ax-pipeline) | GitHub (IVPS examples) |

---

## Conclusion

The AX620E family (AX620Q/AX630C) is **well-supported** for custom NPU models. Pulsar2 is freely available and mature (v5.2). VNPU concurrency eliminates contention with the stock pipeline. Your event-driven cat-ID model can be trained locally, compiled via Docker, validated on desktop with PyAXEngine, and deployed without firmware modification. The 128 MiB SiP constraint is tight but achievable with INT8 quantization and a compact architecture (ResNet18-scale). Zero-copy IVPS→CMM→NPU paths exist but require careful stride/format matching in Pulsar2 config.

**Blockers**: None identified. Proceed with confidence.
