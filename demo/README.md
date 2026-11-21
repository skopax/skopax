# Demo

A Prometheus and a node exporter to try Skopax on, with one alert that you can switch on and off. Nothing here is needed to run Skopax.

- `prometheus.yml` scrapes the node exporter and loads the rules.
- `rules.yml` holds the demo alert `SkopaxDemoExporterUp`. It fires while the exporter runs.

## Start it

Run these from the root of a clone of this repository. The first line makes the secret key once and writes it to `.env`, so run it only once:

    echo SKOPAX_SECRET_KEY="$(openssl rand -hex 32)" >> .env
    docker compose --profile demo up --build

This starts Skopax and its MongoDB, plus the demo Prometheus and node exporter. The demo services publish no ports; Skopax reaches Prometheus at `http://prometheus:9090` inside the compose network.

## Try it

1. Open Skopax at `http://localhost:8080` and create the first admin with the setup code from `docker compose logs skopax`.
2. Add the Prometheus: Connect > Integrations, "+ Add integration" > Prometheus, URL `http://prometheus:9090`, then Test connection and Save & turn on.
3. Within a minute the alert opens a card on the board.
4. Stop the exporter, and the card shows that it stopped firing:

       docker compose stop node-exporter

5. Start it again, and the alert fires again:

       docker compose start node-exporter

## Stop it

    docker compose --profile demo down

Add `-v` to also delete the MongoDB data.

See [Run the demo](../docs/setup.md#run-the-demo) and [Add Prometheus](../docs/setup.md#add-prometheus) in the setup guide.
