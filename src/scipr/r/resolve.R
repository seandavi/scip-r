#!/usr/bin/env Rscript
#
# resolve.R -- the R half of `scip-r resolve`.
#
# Loads an R package from source with pkgload (its dependencies must be
# installed; the package itself need not be), then answers questions the
# syntax-only tree-sitter pass cannot:
#
#   * for every free name used inside the package's functions, which
#     namespace it actually resolves to (imports, base, attached packages)
#     and at what installed version;
#   * which S3 methods the package registers, which S4 generics, classes and
#     methods it defines, and which R6 classes it exports;
#   * a metadata record describing the R environment that produced these
#     answers (R version, platform, every loaded namespace and its version),
#     keyed by a run id that scip-r also stamps into the SCIP index.
#
# It writes a single JSON document; scip-r (Python) merges it into the
# SCIP index. This script has no dependency on scip-r itself and can be
# run standalone:
#
#   Rscript resolve.R --pkg path/to/pkg --out resolved.json [--names names.json]
#   Rscript resolve.R --installed pkgname --out resolved.json [--names names.json]
#
# --pkg loads a source checkout with pkgload (compiles src/ if needed);
# --installed uses loadNamespace() on an already-installed package, which
# is much cheaper on a build machine. --names is an optional JSON array of
# extra names to resolve at namespace level (scip-r passes the names it
# guessed at top level, outside any function, since codetools cannot see
# those).

RESOLVER_VERSION <- "0.2.0"
SCHEMA_VERSION <- 2L

suppressPackageStartupMessages({
  library(methods)
})

for (p in c("pkgload", "codetools", "jsonlite")) {
  if (!requireNamespace(p, quietly = TRUE)) {
    stop(sprintf("resolve.R needs the '%s' package; install.packages('%s')", p, p), call. = FALSE)
  }
}

parse_args <- function(argv) {
  out <- list(pkg = NULL, installed = NULL, out = "resolved.json", names = NULL)
  i <- 1L
  while (i <= length(argv)) {
    a <- argv[[i]]
    if (a %in% c("--pkg", "--installed", "--out", "--names")) {
      if (i == length(argv)) stop("missing value for ", a, call. = FALSE)
      out[[sub("^--", "", a)]] <- argv[[i + 1L]]
      i <- i + 2L
    } else if (a %in% c("-h", "--help")) {
      cat("usage: Rscript resolve.R (--pkg DIR | --installed NAME) --out FILE [--names FILE]\n")
      quit(status = 0)
    } else {
      stop("unknown argument: ", a, call. = FALSE)
    }
  }
  out
}

args <- parse_args(commandArgs(trailingOnly = TRUE))
if (is.null(args$pkg) == is.null(args$installed)) {
  stop("give exactly one of --pkg DIR or --installed NAME", call. = FALSE)
}
started <- Sys.time()

# ---- load the package ------------------------------------------------------

if (!is.null(args$pkg)) {
  pkg_path <- normalizePath(args$pkg, mustWork = TRUE)
  suppressPackageStartupMessages(
    pkgload::load_all(pkg_path, export_all = FALSE, quiet = TRUE, helpers = FALSE)
  )
  pkg_name <- pkgload::pkg_name(pkg_path)
  pkg_version <- as.character(pkgload::pkg_version(pkg_path))
  load_mode <- "load_all"
} else {
  pkg_name <- args$installed
  suppressPackageStartupMessages(loadNamespace(pkg_name))
  pkg_path <- system.file(package = pkg_name)
  pkg_version <- as.character(utils::packageVersion(pkg_name))
  load_mode <- "installed"
}
ns <- asNamespace(pkg_name)
imports_env <- parent.env(ns)

# ---- package manager of an installed package ---------------------------------

manager_of <- local({
  cache <- list()
  function(pkg) {
    if (!is.null(cache[[pkg]])) return(cache[[pkg]])
    desc <- tryCatch(utils::packageDescription(pkg), warning = function(w) NULL, error = function(e) NULL)
    m <- if (is.null(desc)) "." else if (!is.null(desc$biocViews)) "bioconductor" else if (identical(tolower(trimws(desc$Priority %||% "")), "base")) "r" else if (!is.null(desc$Repository) || identical(tolower(trimws(desc$Priority %||% "")), "recommended")) "cran" else "."
    cache[[pkg]] <<- m
    m
  }
})
`%||%` <- function(a, b) if (is.null(a)) b else a
if (!is.null(args$pkg)) {
  own_desc <- read.dcf(file.path(pkg_path, "DESCRIPTION"))
  own_manager <- if ("biocViews" %in% colnames(own_desc)) "bioconductor" else "cran"
} else {
  own_manager <- manager_of(pkg_name)
}

# name -> package, for non-function objects that live in the imports env
import_map <- local({
  imps <- getNamespaceImports(ns)
  m <- character()
  for (from in names(imps)) {
    if (!nzchar(from) || from == "base") next
    what <- imps[[from]]
    if (isTRUE(what)) next # whole-namespace import; handled by topenv below
    m[unname(what)] <- from
  }
  m
})

pkg_version_of <- function(pkg) {
  tryCatch(as.character(utils::packageVersion(pkg)), error = function(e) NA_character_)
}

find_env <- function(name, env) {
  while (!identical(env, emptyenv())) {
    if (exists(name, envir = env, inherits = FALSE)) return(env)
    env <- parent.env(env)
  }
  NULL
}

classify_env <- function(env) {
  if (identical(env, ns)) return("namespace")
  if (identical(env, imports_env)) return("imports")
  if (identical(env, baseenv()) || identical(env, .BaseNamespaceEnv)) return("base")
  if (identical(env, globalenv())) return("global")
  nm <- environmentName(env)
  if (startsWith(nm, "package:")) return("search")
  "other"
}

owning_package <- function(name, obj, env, via) {
  if (is.primitive(obj)) return("base")
  if (is.function(obj)) {
    top <- topenv(environment(obj))
    if (isNamespace(top)) return(unname(getNamespaceName(top)))
    if (identical(top, globalenv())) return(NA_character_)
  }
  switch(via,
    namespace = pkg_name,
    base = "base",
    imports = if (!is.na(import_map[name])) unname(import_map[name]) else NA_character_,
    search = sub("^package:", "", environmentName(env)),
    NA_character_
  )
}

resolve_name <- function(name) {
  env <- find_env(name, ns)
  if (is.null(env)) {
    return(list(name = name, package = NA, version = NA, kind = "unresolved", via = "unresolved"))
  }
  obj <- get(name, envir = env, inherits = FALSE)
  via <- classify_env(env)
  pkg <- owning_package(name, obj, env, via)
  kind <- if (is.function(obj)) "function" else "value"
  list(
    name = name,
    package = pkg,
    version = if (is.na(pkg)) NA else pkg_version_of(pkg),
    manager = if (is.na(pkg)) NA else if (identical(pkg, pkg_name)) own_manager else manager_of(pkg),
    kind = kind,
    via = via
  )
}

# ---- names to resolve --------------------------------------------------------

ns_names <- ls(ns, all.names = TRUE)
ns_names <- ns_names[!startsWith(ns_names, ".__")]
ns_names <- setdiff(ns_names, c(".packageName"))

free_names <- character()
for (nm in ns_names) {
  obj <- get(nm, envir = ns, inherits = FALSE)
  if (!is.function(obj) || is.primitive(obj)) next
  g <- tryCatch(codetools::findGlobals(obj, merge = FALSE), error = function(e) NULL)
  if (is.null(g)) next
  free_names <- c(free_names, g$functions, g$variables)
}
extra_names <- character()
if (!is.null(args$names) && nzchar(args$names) && file.exists(args$names)) {
  extra_names <- as.character(unlist(jsonlite::fromJSON(args$names, simplifyVector = TRUE)))
}
all_names <- sort(unique(c(free_names, extra_names)))
# Operators and syntax pseudo-functions are noise for code intelligence.
all_names <- all_names[grepl("^[A-Za-z.][A-Za-z0-9._]*$", all_names)]

resolutions <- lapply(all_names, resolve_name)

# ---- S3 / S4 / R6 inventory --------------------------------------------------

s3 <- getNamespaceInfo(ns, "S3methods")
s3_methods <- list()
if (is.matrix(s3) && nrow(s3) > 0) {
  for (i in seq_len(nrow(s3))) {
    generic <- s3[i, 1]
    r <- resolve_name(generic)
    s3_methods[[length(s3_methods) + 1L]] <- list(
      system = "S3",
      generic = generic,
      generic_package = r$package,
      generic_version = r$version,
      generic_manager = r$manager,
      class = s3[i, 2],
      method = s3[i, 3]
    )
  }
}

s4_methods <- list()
for (tbl_name in grep("^\\.__T__", ls(ns, all.names = TRUE), value = TRUE)) {
  spec <- sub("^\\.__T__", "", tbl_name)
  generic <- sub(":[^:]*$", "", spec)
  generic_pkg <- sub("^.*:", "", spec)
  tbl <- get(tbl_name, envir = ns)
  for (sig in ls(tbl, all.names = TRUE)) {
    s4_methods[[length(s4_methods) + 1L]] <- list(
      system = "S4",
      generic = generic,
      generic_package = generic_pkg,
      generic_version = pkg_version_of(generic_pkg),
      generic_manager = if (identical(generic_pkg, pkg_name)) own_manager else manager_of(generic_pkg),
      signature = as.list(strsplit(sig, "#", fixed = TRUE)[[1]]),
      method = NA
    )
  }
}

generics <- lapply(methods::getGenerics(where = ns)@.Data, function(g) {
  list(system = "S4", name = g, package = pkg_name)
})

classes <- list()
for (cl in methods::getClasses(where = ns)) {
  def <- methods::getClass(cl, where = ns)
  contains <- lapply(names(def@contains), function(super) {
    ext <- def@contains[[super]]
    spkg <- tryCatch(as.character(ext@package), error = function(e) NA_character_)
    if (length(spkg) != 1 || is.na(spkg) || !nzchar(spkg)) spkg <- NA_character_
    list(
      name = super,
      package = spkg,
      version = if (is.na(spkg)) NA else pkg_version_of(spkg),
      manager = if (is.na(spkg)) NA else if (identical(spkg, pkg_name)) own_manager else manager_of(spkg),
      distance = tryCatch(as.integer(ext@distance), error = function(e) NA_integer_)
    )
  })
  classes[[length(classes) + 1L]] <- list(
    system = if (methods::is(def, "refClassRepresentation")) "RC" else "S4",
    name = cl,
    package = pkg_name,
    contains = contains,
    virtual = isTRUE(def@virtual)
  )
}
for (nm in ns_names) {
  obj <- get(nm, envir = ns, inherits = FALSE)
  if (inherits(obj, "R6ClassGenerator")) {
    inherit <- obj$inherit
    classes[[length(classes) + 1L]] <- list(
      system = "R6",
      name = obj$classname,
      package = pkg_name,
      generator = nm,
      contains = if (is.null(inherit)) list() else list(list(
        name = paste(deparse(inherit), collapse = ""), package = NA, version = NA, manager = NA, distance = 1L
      )),
      virtual = FALSE
    )
  }
}

# ---- environment metadata ----------------------------------------------------

random_hex <- function(n) {
  paste(sample(c(0:9, letters[1:6]), n, replace = TRUE), collapse = "")
}
set.seed(as.integer(Sys.time()) %% .Machine$integer.max + Sys.getpid())
run_id <- random_hex(32)

loaded <- sort(loadedNamespaces())
loaded_packages <- setNames(
  lapply(loaded, function(p) list(version = pkg_version_of(p), manager = if (identical(p, pkg_name)) own_manager else manager_of(p))),
  loaded
)
sysinfo <- Sys.info()
bioc_version <- if (requireNamespace("BiocManager", quietly = TRUE)) {
  tryCatch(as.character(BiocManager::version()), error = function(e) NA_character_)
} else NA_character_

result <- list(
  schema_version = SCHEMA_VERSION,
  run_id = run_id,
  resolver = list(name = "scip-r-resolve", version = RESOLVER_VERSION),
  package = list(name = pkg_name, version = pkg_version, manager = own_manager, path = pkg_path, load_mode = load_mode),
  environment = list(
    timestamp_utc = format(started, "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
    duration_seconds = as.numeric(difftime(Sys.time(), started, units = "secs")),
    r_version = paste(R.version$major, R.version$minor, sep = "."),
    r_version_string = R.version.string,
    platform = R.version$platform,
    os = list(
      sysname = unname(sysinfo[["sysname"]]),
      release = unname(sysinfo[["release"]]),
      machine = unname(sysinfo[["machine"]])
    ),
    locale = Sys.getlocale("LC_COLLATE"),
    bioc_version = bioc_version,
    r_home = R.home(),
    packages = loaded_packages
  ),
  summary = list(
    names_resolved = sum(vapply(resolutions, function(r) r$via != "unresolved", logical(1))),
    names_unresolved = sum(vapply(resolutions, function(r) r$via == "unresolved", logical(1))),
    s3_methods = length(s3_methods),
    s4_methods = length(s4_methods),
    classes = length(classes)
  ),
  resolutions = resolutions,
  generics = generics,
  classes = classes,
  methods = c(s3_methods, s4_methods)
)

writeLines(
  jsonlite::toJSON(result, auto_unbox = TRUE, null = "null", na = "null", pretty = TRUE, digits = NA),
  args$out
)
cat(sprintf(
  "%s: %d names resolved, %d unresolved, %d methods, %d classes (run %s)\n",
  args$out, result$summary$names_resolved, result$summary$names_unresolved,
  length(result$methods), length(classes), run_id
))
