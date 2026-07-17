# Newcastle Live Gig Scraper
It's a script that runs several webscrapers, each on a self-contained python script that searches a particular webpage for music gigs in my city (Newcastle and surroundings), and creates an ics calendar for them.

It then runs a post_processing script that standardises the summaries and merges multiple occurences of the same event using fuzzy logic: it does match reasonably approximate results.

It then identifies recurring events and replaces multiple occurences of the same event on different days by a single recurring event. 

Finally it has a list of filters that creates new calendars only with the venues in there.