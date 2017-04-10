# Imports the codebook, questionnaires, and participants into a non-prod environment.
# Used after setting up a database.

echo "Importing codebook..."
tools/import_codebook.sh $@
echo "Importing questionnaires..."
tools/import_questionnaires.sh $@
echo "Importing participants..."
tools/import_participants.sh $@
