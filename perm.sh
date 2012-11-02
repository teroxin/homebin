cd ~/Sites
find ./ -type d -exec chmod 755 {} \; &>/dev/null
find ./ -type f -exec chmod 644 {} \; &>/dev/null
find ./ -type f -name "*.php" -exec chmod 755 {} \; &>/dev/null

cd ~/Sites/sahana
chmod -R 777 www/tmp &>/dev/null
chmod -R 777 conf &>/dev/null
