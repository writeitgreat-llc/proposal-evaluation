# Wix Integration Guide

Connect your Wix site to the Write It Great proposal evaluation system.

---

## Overview

This integration lets authors submit proposals through a native Wix form.
The form sends data to the Write It Great backend, which runs the AI evaluation
and returns a results URL.

**Architecture:**  Wix Form → Wix Velo (backend) → writeitgreat.com `/api/submit` → AI evaluation

---

## Part 1: Server Configuration

Set these environment variables on your server (e.g. Heroku Settings → Config Vars):

| Variable       | Value                                | Notes                              |
|----------------|--------------------------------------|------------------------------------|
| `API_KEY`      | Generate a strong random token       | e.g. `wIG_api_2026_xK9mP3qR7vLw`  |
| `CORS_ORIGIN`  | `https://www.writeitgreat.com`       | Comma-separate multiple origins    |
| `APP_URL`      | `https://writeitgreat.com`           | Used in email links and API responses |

Generate an API key with:
```bash
python3 -c "import secrets; print('wIG_api_' + secrets.token_urlsafe(24))"
```

---

## Part 2: Wix Setup

### 2a. Create the Page

1. In the Wix Editor, add a new page (e.g. `/proposal`)
2. In Page Settings → SEO → uncheck "Show in sitemap"
3. In Page Settings → uncheck "Show in navigation menu"
4. This keeps the page hidden — only people with the direct link can find it

### 2b. Add Form Elements

Add these elements to the page and set their IDs (in Properties panel):

| Element             | Type                | ID                    |
|---------------------|---------------------|-----------------------|
| Author Name         | Text Input          | `#authorName`         |
| Email               | Text Input          | `#authorEmail`        |
| Book Title          | Text Input          | `#bookTitle`          |
| Proposal Type       | Dropdown            | `#proposalType`       |
| File Upload         | Upload Button       | `#fileUpload`         |
| Ownership Checkbox  | Checkbox            | `#ownershipCheckbox`  |
| Submit Button       | Button              | `#submitButton`       |
| Status Text         | Text                | `#statusText`         |
| Error Text          | Text                | `#errorText`          |

**Dropdown options for `#proposalType`:**
- Label: "Full Proposal" → Value: `full`
- Label: "Marketing Section Only" → Value: `marketing_only`
- Label: "No Marketing Section" → Value: `no_marketing`

**Upload Button settings:**
- File type: Document
- Supported formats: `.pdf, .docx`
- Max file size: 10 MB

### 2c. Store the API Key

1. In the Wix dashboard go to **Developer Tools → Secrets Manager**
2. Create a new secret:
   - Name: `PROPOSAL_API_KEY`
   - Value: (paste the same API_KEY value you set on your server)

### 2d. Add Backend Code

In the Wix Editor, turn on **Dev Mode** (toggle at top).

Create the file `backend/http-functions.js` (or use the Velo sidebar):

**File: `backend/submitProposal.jsw`** (Web Module — runs server-side)

```javascript
// backend/submitProposal.jsw
// This runs on Wix servers (NOT in the browser), so the API key stays secret.

import { fetch } from 'wix-fetch';
import { getSecret } from 'wix-secrets-backend';

const APP_URL = 'https://writeitgreat.com';  // Your backend URL

export async function submitProposal(formData, fileUrl) {
    const apiKey = await getSecret('PROPOSAL_API_KEY');

    // Download the uploaded file from Wix CDN so we can forward it
    const fileResponse = await fetch(fileUrl);
    const fileBuffer = await fileResponse.arrayBuffer();
    const fileName = fileUrl.split('/').pop().split('?')[0] || 'proposal.pdf';

    // Build multipart form data manually (Wix backend doesn't have FormData)
    const boundary = '----WixFormBoundary' + Date.now().toString(36);

    function fieldPart(name, value) {
        return `--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`;
    }

    // Encode text fields
    const textParts = [
        fieldPart('author_name', formData.authorName),
        fieldPart('author_email', formData.authorEmail),
        fieldPart('book_title', formData.bookTitle),
        fieldPart('proposal_type', formData.proposalType || 'full'),
    ].join('');

    // Build binary multipart body
    const textEncoder = new TextEncoder();
    const preamble = textEncoder.encode(textParts + `--${boundary}\r\nContent-Disposition: form-data; name="proposal_file"; filename="${fileName}"\r\nContent-Type: application/octet-stream\r\n\r\n`);
    const epilogue = textEncoder.encode(`\r\n--${boundary}--\r\n`);

    const body = new Uint8Array(preamble.length + fileBuffer.byteLength + epilogue.length);
    body.set(preamble, 0);
    body.set(new Uint8Array(fileBuffer), preamble.length);
    body.set(epilogue, preamble.length + fileBuffer.byteLength);

    const response = await fetch(`${APP_URL}/api/submit`, {
        method: 'POST',
        headers: {
            'Content-Type': `multipart/form-data; boundary=${boundary}`,
            'X-API-Key': apiKey,
        },
        body: body.buffer,
    });

    return response.json();
}
```

### 2e. Add Page Code

On your proposal page, click the `{ }` code icon and add this **Page Code**:

```javascript
// Page Code for the /proposal page
import { submitProposal } from 'backend/submitProposal.jsw';

$w.onReady(function () {
    $w('#statusText').hide();
    $w('#errorText').hide();

    $w('#submitButton').onClick(async () => {
        // Clear previous messages
        $w('#errorText').hide();
        $w('#statusText').hide();

        // Validate fields
        const authorName = $w('#authorName').value.trim();
        const authorEmail = $w('#authorEmail').value.trim();
        const bookTitle = $w('#bookTitle').value.trim();
        const proposalType = $w('#proposalType').value || 'full';
        const ownershipConfirmed = $w('#ownershipCheckbox').checked;

        if (!authorName || !authorEmail || !bookTitle) {
            $w('#errorText').text = 'Please fill in all required fields.';
            $w('#errorText').show();
            return;
        }

        if (!ownershipConfirmed) {
            $w('#errorText').text = 'Please confirm that you own the rights to this work.';
            $w('#errorText').show();
            return;
        }

        // Check file upload
        const uploadedFiles = await $w('#fileUpload').uploadFiles();
        if (!uploadedFiles || uploadedFiles.length === 0) {
            $w('#errorText').text = 'Please upload your proposal document (PDF or DOCX).';
            $w('#errorText').show();
            return;
        }

        const fileUrl = uploadedFiles[0].fileUrl;

        // Show loading state
        $w('#submitButton').disable();
        $w('#submitButton').label = 'Submitting...';
        $w('#statusText').text = 'Evaluating your proposal — this takes about 60 seconds...';
        $w('#statusText').show();

        try {
            const result = await submitProposal(
                { authorName, authorEmail, bookTitle, proposalType },
                fileUrl
            );

            if (result.success) {
                $w('#statusText').text = 'Success! Opening your results...';
                // Open results in new tab
                import('wix-location').then(wixLocation => {
                    wixLocation.to(result.results_url);
                });
            } else {
                $w('#errorText').text = result.error || 'Something went wrong. Please try again.';
                $w('#errorText').show();
                $w('#statusText').hide();
            }
        } catch (err) {
            $w('#errorText').text = 'Connection error. Please check your internet and try again.';
            $w('#errorText').show();
            $w('#statusText').hide();
        } finally {
            $w('#submitButton').enable();
            $w('#submitButton').label = 'Submit Proposal';
        }
    });
});
```

---

## Part 3: Testing

1. **Set server config vars** (`API_KEY`, `CORS_ORIGIN`, `APP_URL`)
2. **Test the API directly** with curl:

```bash
# Replace with your actual values
API_URL="https://writeitgreat.com"
API_KEY="your-api-key-here"

curl -X POST "$API_URL/api/submit" \
  -H "X-API-Key: $API_KEY" \
  -F "author_name=Test Author" \
  -F "author_email=test@example.com" \
  -F "book_title=My Test Book" \
  -F "proposal_type=full" \
  -F "proposal_file=@sample-proposal.pdf"
```

Expected response:
```json
{
  "success": true,
  "submission_id": "WIG-20260217-ABC12",
  "results_url": "https://writeitgreat.com/results/3vY9qkPZbXcN0mW2tR5LgQ",
  "status_url": "https://writeitgreat.com/api/status/3vY9qkPZbXcN0mW2tR5LgQ"
}
```

Note: `results_url` / `status_url` contain an unguessable results token — use
them verbatim. Do not build these URLs from `submission_id`; that is a display
label only, and results pages for new submissions are not served under it.

3. **Publish the Wix site** and test the form end-to-end
4. **Check the admin dashboard** — the submission should appear in the proposal list

---

## Security Summary

| Protection            | How                                                      |
|-----------------------|----------------------------------------------------------|
| API key auth          | `X-API-Key` header required, checked against `API_KEY`   |
| CORS                  | Only `CORS_ORIGIN` domains get CORS headers              |
| Rate limiting         | 10 submissions per IP per hour                           |
| File validation       | PDF/DOCX only, max 10 MB                                |
| Text validation       | Minimum 500 chars extracted text                         |
| Secret storage (Wix)  | API key stored in Wix Secrets Manager, never in browser  |

---

## API Reference

### POST `/api/submit`

**Headers:**
- `X-API-Key` — Required. Must match the `API_KEY` config var.
- `Content-Type` — `multipart/form-data`

**Form Fields:**
| Field           | Required | Description                                         |
|-----------------|----------|-----------------------------------------------------|
| `author_name`   | Yes      | Author's full name                                  |
| `author_email`  | Yes      | Author's email address                              |
| `book_title`    | Yes      | Title of the book                                   |
| `proposal_type` | No       | `full` (default), `marketing_only`, or `no_marketing` |
| `proposal_file` | Yes      | PDF or DOCX file, max 10 MB                         |

**Success Response (200):**
```json
{
  "success": true,
  "submission_id": "WIG-20260217-ABC12",
  "results_url": "https://writeitgreat.com/results/3vY9qkPZbXcN0mW2tR5LgQ",
  "status_url": "https://writeitgreat.com/api/status/3vY9qkPZbXcN0mW2tR5LgQ"
}
```

**Error Responses:**
- `400` — Missing or invalid fields / file
- `401` — Invalid or missing API key
- `429` — Rate limit exceeded
- `500` — Server error
- `503` — API_KEY not configured on server

### GET `/api/status/<submission_id>`

Poll this endpoint to check if evaluation is complete.

```json
{ "status": "processing", "ready": false }
{ "status": "submitted", "ready": true }
```
