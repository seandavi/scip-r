#!/usr/bin/env Rscript
#
# ls_index.R — batch LSP client for `languageserver`, used to resolve call
# sites that a syntax-only pass (scip-r's tree-sitter path) can't: which
# concrete definition a name at a given position actually points to.
#
# IMPORTANT: this script is a design, not verified code. There is no R
# runtime available in the environment that produced it, so the LSP
# request/response plumbing below (built on processx's raw connections)
# has not been executed end to end. Treat the wire-protocol framing and
# request shapes as correct (they follow the published LSP spec and
# languageserver's documented stdio entry point), but test locally —
# `Rscript ls_index.R --pkg path/to/pkg --out test.json` on a real machine
# — before trusting it in CI.
#
# Known limitation, not a bug: this only reliably resolves definitions
# that live in source R can see. For the package being indexed (checked
# out fresh, full R/ present) that's every top-level def. For its
# *dependencies*, resolution depends on whether they were installed with
# source references kept (`options(keep.source.pkgs = TRUE)` at install
# time) — most CRAN binary / RSPM-cached installs do NOT keep these by
# default, so cross-package call sites will often come back with no
# location. If you need that to work, dependencies must be installed
# from source with keep.source.pkgs = TRUE, which costs real CI time
# across a dependency tree. Decide if that trade is worth it before
# relying on this for anything beyond within-package resolution.

suppressPackageStartupMessages({
  library(jsonlite)
  library(processx)
})

parse_args <- function(argv) {
  out <- list(pkg = ".", positions = "", out = "ls-index.json")
  i <- 1
  while (i <= length(argv)) {
    a <- argv[[i]]
    if (a == "--pkg") { out$pkg <- argv[[i + 1]]; i <- i + 2 }
    else if (a == "--positions") { out$positions <- argv[[i + 1]]; i <- i + 2 }
    else if (a == "--out") { out$out <- argv[[i + 1]]; i <- i + 2 }
    else i <- i + 1
  }
  out
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
pkg_root <- normalizePath(args$pkg, mustWork = TRUE)
r_dir <- file.path(pkg_root, "R")
stopifnot(dir.exists(r_dir))

# ---- minimal LSP client -----------------------------------------------

LspClient <- setRefClass(
  "LspClient",
  fields = list(proc = "ANY", con_out = "ANY", next_id = "integer"),
  methods = list(
    initialize_process = function() {
      proc <<- process$new(
        command = "Rscript",
        args = c("-e", "languageserver::run()"),
        stdin = "|", stdout = "|", stderr = "|"
      )
      # processx exposes raw connections for pipe-mode stdio; these are
      # what let us do exact-byte-count reads for the LSP body, since
      # Content-Length framing isn't line-oriented for the payload.
      con_out <<- proc$get_output_connection()
      next_id <<- 1L
    },
    write_message = function(obj) {
      body <- jsonlite::toJSON(obj, auto_unbox = TRUE, null = "null")
      header <- sprintf("Content-Length: %d\r\n\r\n", nchar(body, type = "bytes"))
      proc$write_input(paste0(header, body))
    },
    read_message = function(timeout_s = 30) {
      # Read header lines until the blank line, then read exactly
      # Content-Length bytes for the body.
      deadline <- Sys.time() + timeout_s
      content_length <- NA_integer_
      repeat {
        if (Sys.time() > deadline) stop("timed out waiting for LSP header")
        line <- readLines(con_out, n = 1, warn = FALSE)
        if (length(line) == 0) { Sys.sleep(0.05); next }
        line <- sub("\r$", "", line)
        if (line == "") break
        if (grepl("^Content-Length:", line, ignore.case = TRUE)) {
          content_length <- as.integer(trimws(sub("^Content-Length:", "", line, ignore.case = TRUE)))
        }
      }
      stopifnot(!is.na(content_length))
      body <- readChar(con_out, nchars = content_length, useBytes = TRUE)
      jsonlite::fromJSON(body, simplifyVector = FALSE)
    },
    request = function(method, params) {
      id <- next_id
      next_id <<- next_id + 1L
      write_message(list(jsonrpc = "2.0", id = id, method = method, params = params))
      repeat {
        msg <- read_message()
        if (!is.null(msg$id) && identical(as.integer(msg$id), id)) return(msg)
        # else: a notification (e.g. diagnostics) we don't need — discard.
      }
    },
    notify = function(method, params) {
      write_message(list(jsonrpc = "2.0", method = method, params = params))
    }
  )
)

uri_for <- function(path) paste0("file://", normalizePath(path, mustWork = TRUE))
rel_for <- function(uri, root) {
  p <- sub("^file://", "", uri)
  sub(paste0("^", root, "/?"), "", p)
}

client <- LspClient$new()
client$initialize_process()

init_resp <- client$request("initialize", list(
  processId = NA,
  rootUri = uri_for(pkg_root),
  capabilities = list()
))
client$notify("initialized", list())

r_files <- list.files(r_dir, pattern = "\\.[Rr]$", full.names = TRUE)
file_texts <- setNames(lapply(r_files, function(f) paste(readLines(f, warn = FALSE), collapse = "\n")), r_files)

for (f in r_files) {
  client$notify("textDocument/didOpen", list(textDocument = list(
    uri = uri_for(f), languageId = "r", version = 1L, text = file_texts[[f]]
  )))
}

# --- definitions per file, via documentSymbol (cheap: one request/file) ---
doc_symbols <- list()
for (f in r_files) {
  resp <- client$request("textDocument/documentSymbol", list(textDocument = list(uri = uri_for(f))))
  doc_symbols[[rel_for(uri_for(f), pkg_root)]] <- resp$result
}

# --- targeted call-site resolution, only for positions the tree-sitter
#     pass flagged as unresolved (keeps request count proportional to
#     actual ambiguity, not to every token in the codebase) ---
resolved <- list()
if (nzchar(args$positions) && file.exists(args$positions)) {
  positions <- jsonlite::fromJSON(args$positions, simplifyVector = FALSE)
  for (p in positions) {
    f <- file.path(pkg_root, p$file)
    if (!file.exists(f)) next
    resp <- client$request("textDocument/definition", list(
      textDocument = list(uri = uri_for(f)),
      position = list(line = p$line, character = p$character)
    ))
    resolved[[length(resolved) + 1]] <- list(
      file = p$file, line = p$line, character = p$character,
      definitions = resp$result
    )
  }
}

client$request("shutdown", NULL)
client$notify("exit", NULL)
client$proc$kill()

writeLines(
  jsonlite::toJSON(
    list(package = pkg_root, documentSymbols = doc_symbols, resolvedDefinitions = resolved),
    auto_unbox = TRUE, pretty = TRUE, null = "null"
  ),
  args$out
)
cat(sprintf("wrote %s: %d documents, %d resolved positions\n",
            args$out, length(doc_symbols), length(resolved)))
