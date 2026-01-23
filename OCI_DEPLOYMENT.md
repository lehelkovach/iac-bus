# OCI Deployment (dev + live)

This guide assumes an Ubuntu VM on OCI with a public IP and SSH access.

## Provisioning checklist
1. Create a VM (Ubuntu 22.04 or newer).
2. Open inbound TCP 8091 (dev) or 443 (prod via reverse proxy).
3. Attach your SSH key and note the public IP.

## Bootstrap on the VM
```bash
sudo apt-get update
sudo apt-get install -y git python3-venv
```

## Deploy the bus
```bash
git clone <REPO_URL> iac-bus
cd iac-bus
sudo ./deploy.sh
sudo nano /etc/iac-bus/iac-bus.env
sudo systemctl restart iac-bus.service
sudo systemctl status iac-bus.service
```

## Verify
```bash
curl http://<VM_IP>:8091/health
```

## Optional: TLS in front (recommended for live)
- Use nginx or caddy to terminate TLS on 443 and proxy to 8091.
- Lock down 8091 to the VCN only.

## Info needed to complete live deployment
- VM public IP or DNS name
- SSH user and key path
- Whether to terminate TLS (nginx/caddy) and the domain name
- Desired BUS_API_TOKEN value
