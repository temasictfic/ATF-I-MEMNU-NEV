const fallbackRepo = {
  owner: "temasictfic",
  repo: "Atf-I-Memnu"
};

const technicalAssetPatterns = [/\.blockmap$/i, /^latest.*\.ya?ml$/i];
const endUserExtensionPattern = /\.(exe|msi|zip)$/i;

function inferRepoFromLocation() {
  const host = window.location.hostname;
  const pathParts = window.location.pathname.split("/").filter(Boolean);

  if (host.endsWith(".github.io") && pathParts.length > 0) {
    const owner = host.split(".")[0];
    const repo = pathParts[0];
    return { owner, repo };
  }

  return fallbackRepo;
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value <= 0) {
    return "Unknown size";
  }

  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(size >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatDate(value) {
  if (!value) {
    return "Unknown publish date";
  }

  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) {
    return "Unknown publish date";
  }

  return dt.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderReleaseNotes(notesElement, rawNotes) {
  const notesBody = rawNotes?.trim();
  if (!notesBody) {
    notesElement.textContent = "No release notes provided.";
    return;
  }

  const notesWithEscaping = escapeHtml(notesBody);
  const notesWithChangelogLink = notesWithEscaping.replace(
    /\*\*Full Changelog\*\*:\s*(https:\/\/github\.com\/[^\s<]+)/gi,
    (_match, link) =>
      `<strong>Full Changelog</strong>: <a href="${link}" target="_blank" rel="noreferrer">${link}</a>`
  );

  notesElement.innerHTML = notesWithChangelogLink;
}

function isTechnicalAsset(name) {
  const safeName = name || "";
  return technicalAssetPatterns.some((pattern) => pattern.test(safeName));
}

function classifyAssetKind(name) {
  const safeName = (name || "").toLowerCase();
  if (safeName.includes("setup")) {
    return "setup";
  }
  if (safeName.includes("portable")) {
    return "portable";
  }
  return "other";
}

function isEndUserDownloadAsset(asset) {
  const safeName = asset?.name || "";
  if (!safeName) {
    return false;
  }

  if (isTechnicalAsset(safeName)) {
    return false;
  }

  return endUserExtensionPattern.test(safeName);
}

function getAssetActionHint(kind) {
  if (kind === "setup") {
    return "Recommended. Installs app and adds shortcuts.";
  }
  if (kind === "portable") {
    return "No install needed. Run directly from folder/USB.";
  }
  return "Manual package for advanced use.";
}

function createAssetElement(asset) {
  const row = document.createElement("article");
  row.className = "asset";

  const kind = classifyAssetKind(asset.name);

  const info = document.createElement("div");
  const name = document.createElement("div");
  name.className = "asset-name";
  name.textContent = asset.name || "Unnamed asset";

  if (kind !== "other") {
    const badge = document.createElement("span");
    badge.className = `asset-kind asset-kind-${kind}`;
    badge.textContent = kind === "setup" ? "Setup" : "Portable";
    name.append(" ", badge);
  }

  const meta = document.createElement("div");
  meta.className = "asset-meta";
  meta.textContent = `${formatBytes(asset.size)} - Download count: ${asset.download_count ?? 0}`;

  info.append(name, meta);

  const action = document.createElement("a");
  action.className = "download-btn";
  action.href = asset.browser_download_url;
  action.target = "_blank";
  action.rel = "noreferrer";
  if (kind === "setup") {
    action.textContent = "Download Setup";
  } else if (kind === "portable") {
    action.textContent = "Download Portable";
  } else {
    action.textContent = "Download";
  }

  const actionWrap = document.createElement("div");
  actionWrap.className = "asset-action";

  const actionHint = document.createElement("p");
  actionHint.className = "asset-action-note";
  actionHint.textContent = getAssetActionHint(kind);

  actionWrap.append(actionHint, action);

  row.append(info, actionWrap);
  return row;
}

async function loadLatestRelease() {
  const repoInfo = inferRepoFromLocation();
  const releaseUrl = `https://api.github.com/repos/${repoInfo.owner}/${repoInfo.repo}/releases/latest`;

  const status = document.getElementById("status");
  const releaseRoot = document.getElementById("releaseRoot");
  const repoTag = document.getElementById("repoTag");
  const allReleasesLink = document.getElementById("allReleasesLink");
  const assetFilterInfo = document.getElementById("assetFilterInfo");

  repoTag.textContent = `Repository: ${repoInfo.owner}/${repoInfo.repo}`;
  repoTag.href = `https://github.com/${repoInfo.owner}/${repoInfo.repo}`;
  allReleasesLink.href = `https://github.com/${repoInfo.owner}/${repoInfo.repo}/releases`;

  try {
    const response = await fetch(releaseUrl, {
      headers: {
        Accept: "application/vnd.github+json"
      }
    });

    if (!response.ok) {
      throw new Error(`GitHub API request failed (${response.status})`);
    }

    const release = await response.json();
    const assets = Array.isArray(release.assets) ? release.assets : [];
    const visibleAssets = assets.filter((asset) => isEndUserDownloadAsset(asset));
    const hiddenAssetCount = assets.length - visibleAssets.length;

    document.getElementById("releaseTitle").textContent = release.name || release.tag_name || "Latest release";
    document.getElementById("releaseDate").textContent = `Published ${formatDate(release.published_at)}`;

    const releasePageLink = document.getElementById("releasePageLink");
    releasePageLink.href = release.html_url;

    const notes = document.getElementById("releaseNotes");
    renderReleaseNotes(notes, release.body);

    const assetList = document.getElementById("assetList");
    assetList.innerHTML = "";

    if (hiddenAssetCount > 0) {
      assetFilterInfo.hidden = false;
      assetFilterInfo.textContent = `${hiddenAssetCount} technical updater file(s) hidden (latest*.yml / *.blockmap).`;
    } else {
      assetFilterInfo.hidden = true;
      assetFilterInfo.textContent = "";
    }

    if (visibleAssets.length === 0) {
      const empty = document.createElement("p");
      empty.className = "asset-meta";
      empty.textContent = "No downloadable binary assets were attached to this release.";
      assetList.append(empty);
    } else {
      const sortedAssets = [...visibleAssets].sort((a, b) => {
        const rank = {
          setup: 0,
          portable: 1,
          other: 2
        };
        const rankDiff = rank[classifyAssetKind(a.name)] - rank[classifyAssetKind(b.name)];
        if (rankDiff !== 0) {
          return rankDiff;
        }
        return (b.download_count ?? 0) - (a.download_count ?? 0);
      });
      sortedAssets.forEach((asset) => {
        assetList.append(createAssetElement(asset));
      });
    }

    status.hidden = true;
    releaseRoot.hidden = false;
  } catch (error) {
    status.className = "status status-error";
    status.textContent = `Failed to load latest release. ${error instanceof Error ? error.message : "Unknown error"}`;
    releaseRoot.hidden = true;
  }
}

void loadLatestRelease();
