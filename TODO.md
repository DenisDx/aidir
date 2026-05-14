## example configs:
config.json5 to be removed from git, make config.json5.examle to be copied and filled during the install
add multiple <name>.json5.example configs 
maybe to be selected during install - for different configurations





-----------

To make installation scripts for the configurations:

# Configurations

## aidir + sndbx  + ollama set

perform and test the sequence; guided bypass, multyexectuion
```sh
#install git
sudo apt update && sudo apt install git

#install ollama
curl -fsSL https://ollama.com/install.sh | sh

#install sndbx
cd ~
git clone https://github.com/DenisDx/sndbx.git
# can use git pull git@github.com:DenisDx/sndbx.git
cd sndbx
./install_prerequisites.sh
./install.sh 

#install aidir. Choose example config for this set
cd ~
git clone https://github.com/DenisDx/aidir.git
cd aidir
./install_prerequisites.sh
./install.sh 


```