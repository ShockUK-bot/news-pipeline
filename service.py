[Unit]
Description=c10-scanner Service (trading pipeline)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/pipeline
Environment=PYTHONPATH=/opt/pipeline/src
EnvironmentFile=/etc/pipeline/pipeline.env
ExecStart=/opt/pipeline/.venv/bin/python -m c10_scanner.service
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
