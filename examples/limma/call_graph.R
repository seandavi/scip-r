#!/usr/bin/env Rscript
# Build the internal call graph of a scip-r DuckDB export with igraph.
#
#   Rscript call_graph.R limma.duckdb out/
#
# Writes call_graph.igraph.graphml, call_graph.igraph.png (the busiest 60
# functions, exported ones in blue) and metrics.igraph.csv.

suppressPackageStartupMessages({
  library(duckdb)
  library(igraph)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 2) stop("usage: Rscript call_graph.R DB OUTDIR")
db <- args[[1]]; out <- args[[2]]
dir.create(out, showWarnings = FALSE, recursive = TRUE)

con <- dbConnect(duckdb(), db, read_only = TRUE)
on.exit(dbDisconnect(con, shutdown = TRUE))

pkg <- dbGetQuery(con, "select index_package, index_version from metadata")
edges <- dbGetQuery(con, "
  select caller as from_symbol, symbol as to_symbol, count(*) as weight
  from occurrences
  where caller is not null and not is_definition and not is_local
    and package = index_package
  group by all")
impls <- dbGetQuery(con, "select symbol as from_symbol, related_symbol as to_symbol
                          from relationships where is_implementation")
nodes <- dbGetQuery(con, "select symbol, name, kind, exported from symbols")

short <- function(s) sub("\\(\\)\\.$|\\.$|#$", "", sub("^scip-r \\S+ \\S+ \\S+ ", "", s))

el <- rbind(
  data.frame(from = edges$from_symbol, to = edges$to_symbol, weight = edges$weight, kind = "call"),
  data.frame(from = impls$from_symbol, to = impls$to_symbol, weight = 1, kind = "implements")
)
el <- el[el$from %in% nodes$symbol & el$to %in% nodes$symbol, ]
g <- graph_from_data_frame(el, directed = TRUE, vertices = nodes[, c("symbol", "name", "kind", "exported")])
V(g)$label <- short(V(g)$name)
V(g)$label <- short(names(V(g)))

calls <- subgraph_from_edges(g, E(g)[kind == "call"], delete.vertices = FALSE)
V(g)$in_calls <- strength(calls, mode = "in", weights = E(calls)$weight)
V(g)$out_calls <- degree(calls, mode = "out")
V(g)$pagerank <- page_rank(calls, weights = E(calls)$weight)$vector

write_graph(g, file.path(out, "call_graph.igraph.graphml"), format = "graphml")
metrics <- data.frame(
  fn = V(g)$label, kind = V(g)$kind, exported = V(g)$exported,
  in_calls = V(g)$in_calls, out_calls = V(g)$out_calls, pagerank = round(V(g)$pagerank, 5)
)
metrics <- metrics[order(-metrics$in_calls), ]
write.csv(metrics, file.path(out, "metrics.igraph.csv"), row.names = FALSE)

# plot the busiest 60 functions
top <- head(order(-(V(g)$in_calls + V(g)$out_calls)), 60)
h <- induced_subgraph(g, top)
png(file.path(out, "call_graph.igraph.png"), width = 1800, height = 1400, res = 130)
set.seed(1)
plot(
  h,
  layout = layout_with_fr(h, weights = E(h)$weight),
  vertex.size = 3 + 2 * log1p(V(h)$in_calls),
  vertex.color = ifelse(V(h)$exported, "#9ecae1", "#dddddd"),
  vertex.frame.color = "#555555",
  vertex.label.cex = 0.6, vertex.label.color = "black",
  edge.arrow.size = 0.25, edge.width = 0.4 + log1p(E(h)$weight) / 2,
  edge.color = ifelse(E(h)$kind == "implements", "#c06000", "#99999988"),
  edge.lty = ifelse(E(h)$kind == "implements", 2, 1),
  main = sprintf("%s %s: busiest 60 functions (blue = exported)", pkg$index_package, pkg$index_version)
)
dev.off()
cat(sprintf("%s %s: %d nodes, %d edges (%d implements); wrote %s\n",
            pkg$index_package, pkg$index_version, vcount(g), ecount(g), sum(E(g)$kind == "implements"), out))
