<div align="center">

# Intelligent Document Processor

**An end-to-end Azure-native pipeline that transforms scanned documents into structured, AI-generated summaries - from upload through image enhancement, OCR extraction, to intelligent summarization.**

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2FEldadSomeh%2Fintelligent-document-processor%2Fmain%2Finfrastructure%2Fazuredeploy.json)


</div>




---

## Supported File Formats

| Format | Extension(s) | Notes |
|--------|-------------|-------|
| **PDF** | `.pdf` | Single and multi-page — automatically split into per-page images |
| **JPEG** | `.jpg`, `.jpeg` | |
| **PNG** | `.png` | |
| **TIFF** | `.tif`, `.tiff` | |
| **BMP** | `.bmp` | |
| **WebP** | `.webp` | |

> **Tip:** For best OCR results, upload scanned documents at 200 DPI or higher. The pipeline will automatically upscale low-resolution images when needed.

---

## What This Does

Scanned documents (PDFs, images) are often noisy, skewed, or low-contrast — making OCR unreliable. This solution solves that with a **fully automated, diagnosis-driven pipeline**:

1. **Upload** a document via the web UI or API
2. **Preprocess** — smart image enhancement that only fixes what's broken (skew correction, denoising, contrast adjustment, auto-crop, upscaling)
3. **OCR** — extract text using Azure Document Intelligence with parallel page processing
4. **Summarize** — generate a structured summary using Azure OpenAI with few-shot learning and semantic example matching

The entire pipeline is orchestrated by **Azure Durable Functions** with built-in retry policies and failure handling.

### Key Capabilities

| Feature | Description |
|---------|-------------|
| **Diagnosis-Driven Enhancement** | Only applies fixes for detected problems — clean pages pass through untouched |
| **Region Detection** | Detects stamps, signatures, and tables to protect them during processing |
| **Smart Figure Filtering** | Filters out logos and stamps that would unnecessarily trigger Vision model calls |
| **Parallel Processing** | OCR pages and preprocessing run in parallel for throughput |
| **Few-Shot Learning** | Curated examples injected into each LLM call for consistent output quality |
| **Fine-Tuning** | Train a custom Azure OpenAI model on your examples for permanent style learning |
| **Embedding Re-Ranking** | Examples ranked by semantic similarity using `text-embedding-3-small` vectors |
| **Quality Metrics** | Blur score, estimated DPI, redaction %, OCR readiness — per page |
| **Self-Contained UI** | Single-page React dashboard with drag-and-drop upload, pipeline tracking, settings, and document detail views |

---

## Technology Stack

| Component | Built With | Details |
|-----------|-----------|---------|
| **Dashboard UI** | React 18, Tailwind CSS, shadcn/ui, Lucide icons | Single self-contained HTML file — no separate frontend build or hosting needed. Includes drag-and-drop upload, real-time pipeline tracker, document detail views, settings panel, and few-shot examples manager |
| **Backend API** | Python 3.11, Azure Functions v4 | HTTP-triggered functions for preprocessing, OCR, summarization, upload, and document management. Runs as a Docker container on Linux |
| **Orchestration** | Azure Durable Functions (Python SDK) | Blob-triggered workflow with fan-out/fan-in, activity chaining, and configurable retry policies. Replaces Logic App for lower latency and code-level control |
| **Image Processing** | OpenCV (headless), NumPy, Pillow, pdf2image, poppler-utils | Diagnosis-driven enhancement pipeline — grayscale conversion, auto-crop, denoising, contrast/brightness correction, deskew, upscaling, and region-aware adaptive thresholding |
| **Region Detection** | OpenCV contour analysis + color segmentation | Detects stamps (red/blue hue), signatures, and tables to protect them during binarization |
| **OCR Extraction** | Azure Document Intelligence (prebuilt-layout model) | Parallel per-page processing with structured output — text, tables, figures, and bounding boxes |
| **AI Summarization** | Azure OpenAI (GPT-4o-mini) | Structured summary generation with few-shot in-context learning. System prompt + curated examples injected per call |
| **Smart Figure Analysis** | Azure OpenAI (GPT-4o Vision) | Classifies extracted figures and filters out logos/stamps/headers to avoid unnecessary Vision model calls |
| **Embedding Re-Ranking** | Azure OpenAI (text-embedding-3-small) | Cosine similarity scoring to rank few-shot examples by semantic relevance to the input document |
| **Storage** | Azure Blob Storage, Table Storage, Queue Storage | Blob: raw uploads, enhanced pages, output summaries. Table: Durable Functions orchestration state. Queue: internal messaging |
| **Networking** | VNet, Private Endpoints | VNet isolation with subnet segmentation for Function App and private endpoints |
| **Container Runtime** | Docker (Python 3.11 base image from MCR), Azure Container Registry | Image built automatically during deployment via ACR Tasks; Function App pulls from ACR |
| **Infrastructure as Code** | Bicep → ARM template | One-click deployment of all resources via "Deploy to Azure" button or CLI |

---

## Architecture

The solution runs entirely on Azure, secured within a Virtual Network:

```
Internet (HTTPS)
     │
     ▼
┌─────────────────────┐     ┌──────────────────┐
│   Function App       │────▶│  Blob Storage     │  raw / artifacts / outputs
│   (Docker, Python)   │     └──────────────────┘
│                      │     ┌──────────────────┐
│   • Preprocessing    │────▶│  Table Storage    │  Durable task state
│   • OCR Gateway      │     └──────────────────┘
│   • AI Summarization │     ┌──────────────────┐
│   • Durable Orchestr │────▶│  Doc Intelligence │  OCR extraction
│   • Dashboard UI     │     └──────────────────┘
│   (snet-integration) │     ┌──────────────────┐
└─────────────────────┘────▶│  Azure OpenAI     │  GPT-4o-mini + embeddings
                             └──────────────────┘
```

### Components

| Component | Technology | Purpose |
|-----------|-----------|---------|

| **Orchestrator** | Azure Durable Functions (Python) | Blob trigger → preprocess → OCR → summarize with retry policies |
| **Preprocessing** | Azure Function App (Python 3.11, Docker) | Image enhancement + API + UI |
| **Summarization** | Azure OpenAI (gpt-4o-mini) | Structured summary generation with few-shot examples |
| **Embeddings** | Azure OpenAI (text-embedding-3-small) | Semantic similarity for example re-ranking |
| **Storage** | Azure Blob + Table Storage | Documents, artifacts, outputs, orchestration state |
| **Infrastructure** | Bicep (IaC) | One-click deployment of all resources |

### Networking & Security

- **VNet-isolated** — Storage Account accessible only via private endpoints
- **Function App** accessible directly via its `*.azurewebsites.net` URL with function key authentication
- **Private endpoints** for all storage services (blob, queue, table, file)

---

## Dashboard

The built-in web UI provides:

- **Drag-and-drop upload** with configurable preprocessing settings
- **Real-time pipeline tracker** — 4-step progress (Upload → Preprocess → OCR → Summary)
- **Documents table** — sortable list with quality metrics and status badges
- **Document detail view** — before/after image comparison, quality metrics, enhancement details, region detection, clinical summary, OCR text, extracted tables
- **Few-shot examples manager** — create, edit, and manage examples for summary tuning. Supports drag-and-drop file upload (TXT, DOCX, PDF) or paste text
- **Fine-tuning panel** — export training data, start Azure OpenAI fine-tuning jobs, monitor status, and deploy fine-tuned models
- **Settings panel** — 18 configurable preprocessing parameters

---

## Image Processing Pipeline

The preprocessing is **diagnosis-driven** — it analyses each page and only applies fixes for detected problems:

| Step | Action | Condition |
|------|--------|-----------|
| 0 | Region Detection | Always — detects stamps, signatures, tables |
| 1 | Grayscale Conversion | Always |
| 2 | Auto-Crop | 3-pass: dark borders → white margins → edge-based |
| 3 | Diagnosis | Measures brightness, noise, contrast, skew, dimensions |
| 4 | Denoise | When noise > 8.0 |
| 5a | Brightness Fix | When mean < 140 (gamma + CLAHE) |
| 5b | Contrast Fix | When stddev < 50 (CLAHE) |
| 6 | Adaptive Threshold | Region-aware — preserves stamps/signatures |
| 7 | Deskew | When skew 0.5°–15° |
| 8 | Upscale | When max dimension < 2400px |
| 9 | Dimension Cap | When > 4000px |
| 10 | File Size Safety | When > 3.5MB (Doc Intelligence 4MB limit) |

**Libraries**: OpenCV, NumPy, Pillow, pdf2image, poppler-utils

---

## Durable Functions Orchestration

```
File uploaded to "raw" container
  │
  ▼ blob_pipeline_start (blob trigger)
  │
  ├─ activity_preprocess (gentle mode)
  │
  └─ Switch on recommendedNextAction:
      ├─ "run_doc_intel"                → activity_ocr → activity_summarize
      ├─ "run_doc_intel_low_confidence" → activity_ocr → activity_summarize
      ├─ "retry_stronger"              → activity_preprocess (aggressive) → activity_ocr → activity_summarize
      ├─ "fail"                         → activity_write_failure
      └─ "skip"                         → Done
```

### Retry Policies

| Activity | Max Attempts | First Retry | Back-off |
|----------|-------------|-------------|----------|
| `activity_preprocess` | 3 | 30s | 2.0× |
| `activity_ocr` | 2 | 30s | 2.0× |
| `activity_summarize` | 2 | 10s | 2.0× |

---

## Fine-Tuning

The system supports Azure OpenAI fine-tuning to permanently teach the model your preferred summarization style. This uses the same curated examples from the few-shot examples library as training data.

### How It Works

1. **Curate examples** — Add at least 10 examples via the Examples panel (drag-and-drop files or paste text), or by promoting good summaries
2. **Export training data** — Converts all examples to JSONL format (system prompt + input + ideal output)
3. **Start fine-tuning** — Uploads training data to Azure OpenAI and creates a fine-tuning job
4. **Monitor progress** — Check job status until training completes
5. **Deploy** — Switch the active model to the fine-tuned version

### Few-Shot Examples vs. Fine-Tuning

| | Few-Shot Examples | Fine-Tuning |
|---|---|---|
| **Where knowledge lives** | In the prompt (temporary) | In the model weights (permanent) |
| **Cost per API call** | Higher (examples consume tokens) | Lower (no examples needed) |
| **Change behavior** | Instant — edit examples anytime | Requires retraining |
| **Minimum examples** | 1+ | 10+ (recommended 50+) |

Both approaches can be used together — few-shot examples reinforce the fine-tuned model's learned patterns.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fine-tune/export` | POST | Export all examples as JSONL training data |
| `/api/fine-tune/start` | POST | Upload training data and start a fine-tuning job |
| `/api/fine-tune/status` | GET | Check status of all fine-tuning jobs |
| `/api/fine-tune/deploy` | POST | Switch the active model to a fine-tuned version |

### Configuration

| Parameter | Description |
|-----------|-------------|
| `model` | Base model to fine-tune (default: `gpt-4o-mini-2024-07-18`) |
| `suffix` | Custom name suffix for the fine-tuned model |
| `nEpochs` | Number of training passes (default: 3, recommended: 2–4) |

---

## Getting Started

### What You Need to Prepare (Prerequisites)

Before clicking **Deploy to Azure**, make sure you have the following ready:

| # | Prerequisite | Details |
|---|-------------|---------|
| 1 | **Azure Subscription** | An active subscription with **Owner** or **Contributor + User Access Administrator** role (the template creates role assignments) |
| 2 | **Resource Provider registration** | The following providers must be registered on your subscription: `Microsoft.Web`, `Microsoft.Storage`, `Microsoft.Network`, `Microsoft.ContainerRegistry`, `Microsoft.Insights`, `Microsoft.ManagedIdentity`, `Microsoft.Resources` — most are registered by default; run `az provider register --namespace <name>` if needed |
| 3 | **Azure OpenAI resource** | A deployed Azure OpenAI service with these **model deployments already created**: |
|   | | - **GPT-4o-mini** (or GPT-4o) — for text summarization |
|   | | - **GPT-4o** — for Vision / figure analysis |
|   | | - **text-embedding-3-small** — for semantic example re-ranking |
| 4 | **Azure Document Intelligence resource** | An S0 (or higher) Document Intelligence resource — needed for OCR extraction |
| 5 | **Region with capacity** | Choose a region that has availability for **App Service Plan P1v3** (or B1 for lower cost). Sweden Central, West Europe, East US, and North Europe typically work well |
| 6 | **Azure CLI** (for CLI deployment) | `az` CLI installed and logged in — only needed if deploying via CLI instead of the portal button |

> **Note:** Azure OpenAI and Document Intelligence are **not** created by the template — you must provision them separately and provide their endpoints/keys after deployment (see Step 2 below).

### What the Deployment Creates Automatically

The Bicep template (one-click **Deploy to Azure** button) provisions and configures **all** of the following — no manual resource creation needed:

| Resource | Type / SKU | What It Does |
|----------|-----------|--------------|
| **Storage Account** | Standard_LRS | Blob containers (`raw`, `artifacts`, `outputs`), Table Storage (Durable Functions state), Queue Storage (internal messaging) |
| **Virtual Network** | 10.0.0.0/16 (2 subnets) | `snet-integration` (Function App), `snet-pe` (Private Endpoints) |
| **Private DNS Zones** | 4 zones | For blob, file, table, and queue private endpoint resolution |
| **Private Endpoints** | 4 endpoints | Secures Storage Account access over the VNet (blob, file, table, queue) |
| **Azure Container Registry** | Basic | Stores the Docker image for the Function App |
| **Application Insights** | — | Telemetry, logging, and monitoring for the Function App |
| **App Service Plan** | P1v3 Linux | Hosts the Function App (configurable via `funcPlanSku` / `funcPlanTier` parameters) |
| **Function App** | Linux, Docker container | Runs the entire pipeline — preprocessing, OCR gateway, summarization, Durable orchestrator, and dashboard UI |
| **User-Assigned Managed Identity** | — | Used by the deployment script to build and push the Docker image to ACR |
| **Role Assignments** | 5 assignments | Storage Blob Data Owner, Queue Data Contributor, Table Data Contributor (Function App), AcrPush + Contributor (build identity) |
| **ACR Build Task** | Deployment Script | Builds the Docker image from the GitHub repo directly in ACR (no local Docker needed) |

> **Total resources created: ~20** (including subnets, DNS zone links, and role assignments)

### Deployment Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `location` | Resource group location | Azure region |
| `projectName` | `preocr` | Prefix for all resource names |
| `env` | `dev` | Environment suffix (`dev`, `staging`, `prod`) |
| `funcPlanSku` | `P1v3` | App Service Plan SKU — use `B1` for lower cost |
| `funcPlanTier` | `PremiumV3` | App Service Plan tier — use `Basic` with `B1` |
| `sourceRepoUrl` | This repo | Git URL for ACR build |
| `vnetAddressPrefix` | `10.0.0.0/16` | VNet address space |

### 1. Deploy Infrastructure + Code

The Bicep template deploys all Azure resources **and** automatically builds the Docker image from this repo into ACR:

```bash
# Login to Azure
az login

# Create a resource group (pick a region with available capacity)
az group create --name rg-docprocessor --location westeurope

# Deploy infrastructure + auto-build code
az deployment group create \
  --resource-group rg-docprocessor \
  --template-file infrastructure/main.bicep \
  --parameters projectName=docproc env=dev
```

Or click the **Deploy to Azure** button at the top of this page.

> The deployment takes ~15 minutes. It creates all Azure resources, builds the Docker image in ACR, and configures the Function App to pull from it automatically.

> **Important:** If you get a **"No available instances"** error, this is an Azure regional capacity issue. Try:
> 1. Deploy to a **different region** (e.g., `westeurope`, `eastus`, `northeurope`)
> 2. Use a **lower-cost SKU** by adding: `--parameters funcPlanSku=B1 funcPlanTier=Basic`
> 3. Create a **new resource group** (capacity is allocated per stamp)

### 2. Configure App Settings (Required Post-Deployment Step)

The deployment creates all infrastructure but **does not provision Azure OpenAI or Document Intelligence** — these must exist beforehand. After deployment, configure the Function App with your AI service credentials:

```bash
# Get your function app name (it includes a random suffix)
az functionapp list --resource-group rg-docprocessor --query "[].name" -o tsv

# Set required AI service settings
az functionapp config appsettings set \
  --resource-group rg-docprocessor \
  --name <your-func-app-name> \
  --settings \
    "DOC_INTEL_ENDPOINT=https://<your-docintel>.cognitiveservices.azure.com/documentintelligence/documentModels/prebuilt-layout:analyze?api-version=2024-11-30" \
    "DOC_INTEL_KEY=<your-doc-intel-key>" \
    "AZURE_OPENAI_ENDPOINT=https://<your-openai>.openai.azure.com/" \
    "AZURE_OPENAI_KEY=<your-openai-key>" \
    "AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini" \
    "AZURE_OPENAI_VISION_DEPLOYMENT=gpt-4o" \
    "AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small"
```

> **Note:** `STORAGE_ACCOUNT_URL` and `STORAGE_ACCOUNT_KEY` are automatically set by the template — you don't need to configure them.

### 3. Access the Application

> **Important:** The dashboard is served at the **`/api/ui`** path — not the root URL.

After deployment completes, open the **dashboardUrl** from the deployment outputs:
```
https://<your-func-app-name>.azurewebsites.net/api/ui
```

The function key is automatically injected into the page by the server, so no `?code=` parameter is needed.

Alternatively, you can get the URL from the CLI:
```bash
# Get the dashboard URL
az deployment group show -g <rg> -n <deployment-name> --query "properties.outputs.dashboardUrl.value" -o tsv

# Or construct it manually
echo "https://$(az functionapp show -g <rg> -n <func-app-name> --query defaultHostName -o tsv)/api/ui"
```

### 4. Verify Everything Works

| Check | How |
|-------|-----|
| **Dashboard loads** | Browse to `https://<func-app>.azurewebsites.net/api/ui` — should show the PreOCR Lab UI |
| **Upload works** | Drag-and-drop a PDF or image into the upload zone |
| **Pipeline completes** | Watch the 4-step tracker: Upload → Preprocess → OCR → Summary |

---

## Project Structure

```
intelligent-document-processor/
├── function-app/
│   ├── Dockerfile                    # Python 3.11 + poppler-utils
│   ├── function_app.py               # All HTTP endpoints + Durable orchestration
│   ├── requirements.txt              # Python dependencies
│   ├── host.json                     # Function App configuration
│   ├── local.settings.json.example   # Template for local development
│   ├── static/
│   │   ├── ui.html                   # Built React dashboard (single-page)
│   │   └── index.html                # Legacy dashboard
│   └── preprocessing/
│       ├── image_processor.py        # Image enhancement pipeline
│       ├── metrics.py                # Quality metrics & decision logic
│       ├── models.py                 # PreprocessOptions dataclass
│       ├── blob_helper.py            # Azure Blob Storage operations
│       ├── pdf_handler.py            # PDF → PNG page splitting
│       ├── region_detector.py        # Stamp/signature/table detection
│       └── auto_tuner.py             # Automatic parameter tuning
├── infrastructure/
│   ├── main.bicep                    # All Azure resources (IaC)
│   └── azuredeploy.json              # ARM template (generated from Bicep)
└── docs/
    └── images/
        └── architecture.svg          # Architecture diagram
```

---

## API Reference

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/preprocess` | POST | Function key | Process pages, return quality metrics |
| `/api/ocr` | POST | Function key | Multi-page OCR via Document Intelligence |
| `/api/summarize` | POST | Function key | AI summarization via Azure OpenAI |
| `/api/upload` | POST | Function key | File upload with optional settings |
| `/api/documents` | GET | Function key | List all processed documents |
| `/api/documents/{docId}` | GET | Function key | Single document detail |
| `/api/image/{docId}/{page}` | GET | Function key | Serve enhanced page image |
| `/api/summary/{docId}` | GET | Function key | Retrieve saved summary |
| `/api/examples` | GET/POST | Function key | Manage few-shot examples |
| `/api/examples/upload` | POST | Function key | Create example from uploaded files (TXT, DOCX, PDF) |
| `/api/promote-to-example` | POST | Function key | Promote a document to a golden example |
| `/api/pipeline-status/{id}` | GET | Function key | Query orchestration status |
| `/api/ui` | GET | Anonymous | Dashboard UI |

---

## Few-Shot Example System

The summarization uses **in-context learning** — curated (input, ideal-summary) pairs are injected into each LLM call:

| Strategy | Description |
|----------|-------------|
| **Golden flag** | Manually verified examples get a 0.3 score bonus |
| **Type matching** | Same document category preferred |
| **Recency** | Newer examples break ties |
| **Semantic similarity** | Cosine similarity via `text-embedding-3-small` vectors |

Examples are stored in `artifacts/examples/{id}/` with `input.txt`, `summary.txt`, `metadata.json`, and `embedding.json`.

### Adding Examples

There are three ways to add training examples:

| Method | Description |
|--------|-------------|
| **Drag-and-drop files** | Drop an input document + ideal summary file (TXT, DOCX, or text-based PDF) into the "Add Example" dialog — no scanning or OCR needed |
| **Paste text** | Toggle to paste mode and type/paste the input text and ideal summary directly |
| **Promote a processed document** | Click "Save as Example" on any document's Summary tab to save the OCR text + AI summary as a training pair |

> **Tip:** Drag-and-drop is the fastest way for doctors to add training examples — just drop the original document and the corrected summary, and the system extracts the text automatically.

---

## Local Development

1. Clone the repo:
   ```bash
   git clone https://github.com/EldadSomeh/intelligent-document-processor.git
   cd intelligent-document-processor/function-app
   ```

2. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Linux/macOS
   .venv\Scripts\activate      # Windows
   pip install -r requirements.txt
   ```

3. Copy and edit local settings:
   ```bash
   cp local.settings.json.example local.settings.json
   # Edit local.settings.json with your Azure service endpoints and keys
   ```

4. Install Azure Functions Core Tools and run:
   ```bash
   func start
   ```

> **Note:** PDF processing requires `poppler-utils`. Install via:
> - **Ubuntu/Debian**: `sudo apt-get install poppler-utils`
> - **macOS**: `brew install poppler`
> - **Windows**: Download from [poppler releases](https://github.com/ospadber/poppler-windows/releases)

---

## Quality Metrics

Each processed page is scored on:

| Metric | Method | Purpose |
|--------|--------|---------|
| **Blur Score** | Laplacian variance | Higher = sharper |
| **Estimated DPI** | Pixel dimension analysis | Target: ≥200 DPI |
| **Redaction %** | Black rectangle detection | High = unusable |
| **OCR Readiness** | Composite score | `0.4 × blur + 0.3 × contrast + 0.3 × redaction` |

### Decision Logic

| Condition | Action |
|-----------|--------|
| blur < 15 or redaction > 90% | Fail |
| blur < 50 or redaction > 70% or readiness < 0.30 | OCR with low confidence |
| readiness < 0.50 | Retry with aggressive preprocessing |
| Otherwise | Send to OCR |

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -am 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

## Acknowledgements

Built with:
- [Azure Functions](https://learn.microsoft.com/azure/azure-functions/) — Serverless compute
- [Azure Durable Functions](https://learn.microsoft.com/azure/azure-functions/durable/) — Workflow orchestration
- [Azure Document Intelligence](https://learn.microsoft.com/azure/ai-services/document-intelligence/) — OCR extraction
- [Azure OpenAI](https://learn.microsoft.com/azure/ai-services/openai/) — AI summarization
- [OpenCV](https://opencv.org/) — Image processing
- [React](https://react.dev/) + [Tailwind CSS](https://tailwindcss.com/) + [shadcn/ui](https://ui.shadcn.com/) — Dashboard UI

---

<div align="center">

**Built by Microsoft architects for document processing at scale.**

*Deploy with one click. Process documents in minutes.*

</div>
